#!/usr/bin/env python3
"""一次性补传工具：确保本地缓存目录中的所有文件都已成功上传到 WebDAV 远端。

- 从 /app/session/webdav.json 读取真实运行配置（URL/USER/PASS）
- 对每个本地文件先 PROPFIND 校验远端是否已存在且大小一致（省流量）
- 缺失/不一致的用 src.webdav.upload_file 上传（含完整性校验、无进度看门狗）

用法（容器内）:
    python3 scripts/ensure_webdav.py <local_dir> <remote_dir>
例:
    python3 scripts/ensure_webdav.py /app/downloads/job-1786808942 115/Pron/2026-08-16/3
"""

import base64
import http.client
import json
import logging
import os
import re
import ssl
import sys
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ensure_webdav")

CFG_FILE = "/app/session/webdav.json"


def load_cfg() -> dict:
    with open(CFG_FILE) as f:
        data = json.load(f)
    cfg = {
        "url": data.get("url", ""),
        "user": data.get("user", ""),
        "pass": data.get("pass", ""),
        "retry": int(data.get("retry", 5)),
    }
    if not cfg["url"]:
        sys.exit("webdav.json 缺少 url")
    return cfg


def remote_size(base_url: str, remote_dir: str, name: str, user: str, passwd: str) -> int | None:
    """PROPFIND Depth:0 查远端文件大小；不存在/失败返回 None。"""
    parsed = urllib.parse.urlsplit(base_url)
    rel = f"{remote_dir.strip('/')}/{name}"
    target = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            urllib.parse.quote(f"{parsed.path.rstrip('/')}/{rel}", safe="/"),
            "",
            "",
        )
    )
    auth = "Basic " + base64.b64encode(f"{user}:{passwd}".encode()).decode()
    try:
        conn = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=30,
            context=ssl.create_default_context(),
        )
        try:
            conn.request(
                "PROPFIND", target,
                headers={"Authorization": auth, "Depth": "0"},
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


def _remove_local(path: str) -> None:
    """删除本地缓存文件（确认远端完整后调用）。"""
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("删除本地缓存失败 %s: %s", path, exc)


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: ensure_webdav.py <本地目录> <远端目录>")
        return 2
    local_dir, remote_dir = sys.argv[1], sys.argv[2]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    from src import webdav

    cfg = load_cfg()
    # 持久循环：直到全部文件成功上传（openlist/网络偶发失败会自动重试）
    round_no = 0
    while True:
        round_no += 1
        print(f"===== 第 {round_no} 轮开始 =====", flush=True)
        files = sorted(
            f for f in os.listdir(local_dir)
            if os.path.isfile(os.path.join(local_dir, f))
        )
        print(f"本地文件: {len(files)} 个，远端目录: {remote_dir}", flush=True)
        ok = skip = fail = 0
        for name in files:
            local = os.path.join(local_dir, name)
            size = os.path.getsize(local)
            rs = remote_size(cfg["url"], remote_dir, name, cfg["user"], cfg["pass"])
            if rs is not None and rs == size:
                skip += 1
                _remove_local(local)
                continue
            print(f"[上传] {name} ({size}) 远端={rs}", flush=True)
            res = webdav.upload_file(
                cfg["url"], remote_dir, local,
                cfg["user"], cfg["pass"],
                retries=max(0, cfg["retry"] - 1),
            )
            if not res:
                # 上传返回失败，但 openlist 可能在尾部已实际落盘——PROPFIND 兜底校验
                rs2 = remote_size(cfg["url"], remote_dir, name, cfg["user"], cfg["pass"])
                if rs2 is not None and rs2 == size:
                    logger.warning("%s: 上传报告失败但远端已完整（%d），视为成功", name, size)
                    res = True
            if res:
                ok += 1
                _remove_local(local)
                print(f"  -> OK（已删本地缓存）", flush=True)
            else:
                fail += 1
                print(f"  -> FAIL（下轮重试，本地缓存保留）", flush=True)
        print(f"第 {round_no} 轮完成: 新传 {ok} / 已存在 {skip} / 失败 {fail}", flush=True)
        if fail == 0:
            print("全部文件上传完成", flush=True)
            return 0
        print(f"{fail} 个文件仍失败，5 分钟后重试…", flush=True)
        time.sleep(300)


if __name__ == "__main__":
    sys.exit(main())
