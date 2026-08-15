"""WebDAV 上传模块（纯标准库，无额外依赖）。

下载完成后把本地文件上传到 WebDAV 服务器：
    <base_url>/<remote_dir>/<日期文件夹>/<文件名>
阻塞 IO 走线程池（asyncio.to_thread），不卡事件循环。
"""

import base64
import http.client
import logging
import os
import ssl
import urllib.parse

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_REDIRECT_MAX = 3
_TIMEOUT = 3600


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
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
            if size and sent % (8 * _CHUNK) == 0:
                logger.info("WebDAV upload %s: %d/%d bytes", url_path, sent, size)
    resp = conn.getresponse()
    resp.read()
    if resp.status in (200, 201, 204):
        logger.info("WebDAV uploaded %s (%d bytes)", url_path, size)
        return True
    logger.error("WebDAV PUT %s -> %s", url_path, resp.status)
    return False


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
