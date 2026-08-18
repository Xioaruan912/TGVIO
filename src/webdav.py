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
# PUT 数据发送完成后，等待服务器响应的最长时间（openlist 对大文件 PUT 响应慢，
# 超时后改用 PROPFIND 轮询确认远端完整性，而不是干等后重传整个文件）
_RESP_TIMEOUT = 30
# PROPFIND 轮询确认次数与间隔（openlist 接收后后台转存，需等其完成）
_VERIFY_ATTEMPTS = 36
_VERIFY_INTERVAL = 10


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


def _put_file(
    conn: http.client.HTTPConnection,
    parsed,
    url_path: str,
    local_path: str,
    auth: str,
    progress_callback=None,
) -> bool:
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
                if progress_callback:
                    progress_callback(sent, size)
    # 数据已发送完成。部分 WebDAV 服务（如 openlist 转发上游）对大文件 PUT 响应很慢，
    # 响应等待只给 _RESP_TIMEOUT 秒；超时/异常不当作失败，改用 PROPFIND 轮询确认远端完整性。
    try:
        conn.sock.settimeout(_RESP_TIMEOUT)
        resp = conn.getresponse()
        resp.read()
        if resp.status in (200, 201, 204):
            return _verify_remote(parsed, url_path, size, auth)
        # 非成功状态（423 Locked=后台转存中，或其它）也可能最终落盘——先轮询确认
        logger.warning(
            "WebDAV PUT %s -> %s，PROPFIND 轮询确认…", url_path, resp.status
        )
        return _verify_remote(parsed, url_path, size, auth)
    except Exception as exc:
        logger.warning(
            "WebDAV PUT %s 响应等待超时(%s)，PROPFIND 轮询确认…",
            url_path,
            exc.__class__.__name__,
        )
        return _verify_remote(parsed, url_path, size, auth)


def _verify_remote(parsed, url_path: str, size: int, auth: str, attempts: int = None, interval: float = None) -> bool:
    """PROPFIND 轮询确认远端文件大小 == 本地大小（防假成功；兼容服务器响应慢）。"""
    attempts = _VERIFY_ATTEMPTS if attempts is None else attempts
    interval = _VERIFY_INTERVAL if interval is None else interval
    for _ in range(attempts):
        rs = _remote_size(parsed, url_path, auth)
        if rs is not None and rs == size:
            logger.info("WebDAV uploaded %s (%d bytes)", url_path, size)
            return True
        time.sleep(interval)
    logger.error("WebDAV 完整性校验失败 %s: 远端未确认完整", url_path)
    return False


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


def _upload_once(
    base_url: str,
    remote_dir: str,
    local_path: str,
    user: str,
    passwd: str,
    remote_name: str = "",
    progress_callback=None,
) -> bool:
    """单次上传（不重试）。remote_dir 为相对 dav 根的目录路径（自动创建日期文件夹）。"""
    auth = _auth_header(user, passwd)
    parsed, root_path = _split(base_url)
    name = remote_name or os.path.basename(local_path)
    rel_dir = f"{remote_dir.strip('/')}/{name}"
    url_path = f"{root_path.rstrip('/')}/{rel_dir}"
    conn = _connect(parsed)
    try:
        _mkcol(conn, parsed, f"{root_path.rstrip('/')}/{remote_dir.strip('/')}", auth)
        return _put_file(conn, parsed, url_path, local_path, auth, progress_callback)
    finally:
        conn.close()


def upload_file(
    base_url: str,
    remote_dir: str,
    local_path: str,
    user: str,
    passwd: str,
    retries: int = 2,
    remote_name: str = "",
    progress_callback=None,
) -> bool:
    """上传 local_path 到 <base_url>/<remote_dir>/<文件名>，失败自动重试 retries 次。

    默认远端文件名 = 本地 basename；传 remote_name 可自定义（如 hash 名）。
    重试间隔较长（60s），给后端（如 openlist 转存上游）时间完成落盘，避免 423 锁冲突。
    progress_callback(sent, size) 每约 8MB 调用一次（发送循环内，线程上下文）。
    """
    for attempt in range(retries + 1):
        try:
            if _upload_once(base_url, remote_dir, local_path, user, passwd, remote_name, progress_callback):
                return True
        except Exception as exc:
            logger.warning("WebDAV upload attempt %d failed: %s", attempt + 1, exc)
        if attempt < retries:
            time.sleep(60)
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
