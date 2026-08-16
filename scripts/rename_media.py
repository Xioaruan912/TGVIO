#!/usr/bin/env python3
"""批量重命名媒体文件为 <文件内容MD5前8位>.<后缀>（含视频/图片）。

- 分块读取计算 MD5，大文件不占内存
- 冲突（hash 前 8 位相同）保留原名并告警
- 用法: python3 scripts/rename_media.py <目录>
"""

import hashlib
import os
import sys

_CHUNK = 1024 * 1024


def file_md5_short(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            c = f.read(_CHUNK)
            if not c:
                break
            h.update(c)
    return h.hexdigest()[:8]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: rename_media.py <目录>")
        return 2
    d = sys.argv[1]
    renamed = 0
    conflicts = 0
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        stem, ext = os.path.splitext(name)
        h = file_md5_short(p)
        new = f"{h}{ext}"
        if new == name:
            print(f"[已是 hash 名] {name}")
            continue
        np = os.path.join(d, new)
        if os.path.exists(np):
            print(f"[冲突] {name} -> {new}（已存在，保留原名）")
            conflicts += 1
            continue
        os.rename(p, np)
        renamed += 1
        print(f"{name} -> {new}")
    print(f"\n完成: 重命名 {renamed} 个 / 冲突 {conflicts} 个")
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
