"""WebDAV 上传模块（纯标准库，无额外依赖）。

下载完成后把本地文件上传到 WebDAV 服务器：
    <base_url>/<remote_dir>/<日期文件夹>/<文件名>
阻塞 IO 走线程池（asyncio.to_thread），不卡事件循环。
"""

import base64
import http.client
import logging
import os
import re
import ssl
import time
import urllib.parse

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_REDIRECT_MAX = 3
# socket 级超时：单次 send/recv 操作阻塞上限（覆盖"服务器不响应"卡死，如 PUT 尾部）
_TIMEOUT = 300
# 发送循环无进度看门狗：超过该秒数无新字节上传则主动中断（触发重试）
_STALL_TIMEOUT = 120


def _auth_header(user: str, passwd: str) -> str:
    token = base64.b64encode(f"{user}:{passwd}".encode()).decode()
    return f"Basic {token}"


def _split(url: str):
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path) or "/"
    return parsed, path


def _connect(parsed) -> http.client.HTTPConnection:
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=_TIMEOUT,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=_TIMEOUT)


def _mkcol(conn: http.client.HTTPConnection, parsed, dir_path: str, auth: str) -> bool:
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, urllib.parse.quote(dir_path, safe="/"), "", "")
    )
    conn.request("MKCOL", target, headers={"Authorization": auth})
    resp = conn.getresponse()
    resp.read()
    if resp.status in (200, 201, 204):
        return True
    if resp.status == 405:
        logger.debug("WebDAV dir already exists: %s", dir_path)
        return True
    logger.warning("WebDAV MKCOL %s -> %s", dir_path, resp.status)
    return False


def _put_file(conn: http.client.HTTPConnection, parsed, url_path: str, local_path: str, auth: str) -> bool:
    size = os.path.getsize(local_path)
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, urllib.parse.quote(url_path, safe="/"), "", "")
    )
    conn.putrequest("PUT", target, skip_accept_encoding=True)
    conn.putheader("Authorization", auth)
    conn.putheader("Content-Length", str(size))
    conn.putheader("Content-Type", "application/octet-stream")
    conn.endheaders()
    sent = 0
    last_sent = time.monotonic()
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
            if time.monotonic() - last_sent > _STALL_TIMEOUT:
                raise TimeoutError(
                    f"WebDAV 上传无进度超过 {_STALL_TIMEOUT}s，中断重试"
                )
            last_sent = time.monotonic()
            if size and sent % (8 * _CHUNK) == 0:
                logger.info("WebDAV upload %s: %d/%d bytes", url_path, sent, size)
    resp = conn.getresponse()
    resp.read()
    if resp.status not in (200, 201, 204):
        logger.error("WebDAV PUT %s -> %s", url_path, resp.status)
        return False
    # 完整性校验：远端文件大小必须与本地一致，防止"假成功"（静默丢失）
    remote_size = _remote_size(parsed, url_path, auth)
    if remote_size is not None and remote_size != size:
        logger.error(
            "WebDAV 完整性校验失败 %s: 远端 %d != 本地 %d",
            url_path,
            remote_size,
            size,
        )
        return False
    logger.info("WebDAV uploaded %s (%d bytes)", url_path, size)
    return True


def _remote_size(parsed, url_path: str, auth: str) -> int | None:
    """PROPFIND（Depth:0）查询远端文件大小；失败返回 None（不阻断，视为无法校验）。"""
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, urllib.parse.quote(url_path, safe="/"), "", "")
    )
    body = (
        '<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
        "<D:prop><D:getcontentlength/></D:prop></D:propfind>"
    )
    try:
        conn = _connect(parsed)
        try:
            conn.request(
                "PROPFIND",
                target,
                body=body,
                headers={
                    "Authorization": auth,
                    "Depth": "0",
                    "Content-Type": "application/xml",
                },
            )
            resp = conn.getresponse()
            data = resp.read()
            if resp.status in (200, 207):
                m = re.search(rb"<D:getcontentlength>(\d+)</D:getcontentlength>", data)
                if m:
                    return int(m.group(1))
            return None
        finally:
            conn.close()
    except Exception:
        return None


def _upload_once(base_url: str, remote_dir: str, local_path: str, user: str, passwd: str) -> bool:
    """单次上传（不重试）。remote_dir 为相对 dav 根的目录路径（自动创建日期文件夹）。"""
    auth = _auth_header(user, passwd)
    parsed, root_path = _split(base_url)
    rel_dir = f"{remote_dir.strip('/')}/{os.path.basename(local_path)}"
    url_path = f"{root_path.rstrip('/')}/{rel_dir}"
    conn = _connect(parsed)
    try:
        _mkcol(conn, parsed, f"{root_path.rstrip('/')}/{remote_dir.strip('/')}", auth)
        return _put_file(conn, parsed, url_path, local_path, auth)
    finally:
        conn.close()


def upload_file(
    base_url: str,
    remote_dir: str,
    local_path: str,
    user: str,
    passwd: str,
    retries: int = 2,
) -> bool:
    """上传 local_path 到 <base_url>/<remote_dir>/<文件名>，失败自动重试 retries 次。"""
    for attempt in range(retries + 1):
        try:
            if _upload_once(base_url, remote_dir, local_path, user, passwd):
                return True
        except Exception as exc:
            logger.warning("WebDAV upload attempt %d failed: %s", attempt + 1, exc)
    logger.error("WebDAV upload failed after %d attempts: %s", retries + 1, local_path)
    return False


def delete_remote(
    base_url: str,
    remote_dir: str,
    filename: str,
    user: str,
    passwd: str,
    retries: int = 2,
) -> bool:
    """删除 <base_url>/<remote_dir>/<文件名>（逐文件删除，不删目录）。

    远端 404（文件本就不存在）视为删除成功。
    """
    auth = _auth_header(user, passwd)
    parsed, root_path = _split(base_url)
    rel = f"{remote_dir.strip('/')}/{filename}"
    target = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            urllib.parse.quote(f"{root_path.rstrip('/')}/{rel}", safe="/"),
            "",
            "",
        )
    )
    for attempt in range(retries + 1):
        try:
            conn = _connect(parsed)
            try:
                conn.request("DELETE", target, headers={"Authorization": auth})
                resp = conn.getresponse()
                resp.read()
                if resp.status in (200, 204, 404):
                    return True
                logger.warning("WebDAV DELETE %s -> %s", target, resp.status)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("WebDAV DELETE attempt %d failed: %s", attempt + 1, exc)
    logger.error("WebDAV DELETE failed after %d attempts: %s", retries + 1, target)
    return False


def ensure_dir(base_url: str, remote_dir: str, user: str, passwd: str) -> None:
    """确保远端目录存在（上传时会自动创建，仅日志用途）。"""
    try:
        auth = _auth_header(user, passwd)
        parsed, root_path = _split(base_url)
        conn = _connect(parsed)
        try:
            _mkcol(conn, parsed, f"{root_path.rstrip('/')}/{remote_dir.strip('/')}", auth)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("WebDAV ensure_dir %s failed: %s", remote_dir, exc)
