#!/usr/bin/env bash
# Switch the Player's archive read mode (the same setting as Settings > read mode).
# Usage: bash scripts/player_read_mode.sh direct|webdav
set -Eeuo pipefail
mode=${1:?direct or webdav}
docker exec -i tgvio-player python - "$mode" <<'EOF'
import json, os, sys, urllib.request
base = "http://127.0.0.1:8790"
login = urllib.request.Request(
    base + "/api/v1/auth/login",
    json.dumps({"secret": os.environ["TGVIO_PLAYER_ACCESS_SECRET"]}).encode(),
    {"Content-Type": "application/json"},
)
cookie = urllib.request.urlopen(login, timeout=30).headers["Set-Cookie"].split(";", 1)[0]
put = urllib.request.Request(
    base + "/api/v1/settings/read-mode", json.dumps({"mode": sys.argv[1]}).encode(),
    {"Content-Type": "application/json", "Cookie": cookie}, method="PUT",
)
urllib.request.urlopen(put, timeout=30).read()
get = urllib.request.Request(base + "/api/v1/settings/read-mode", headers={"Cookie": cookie})
print("read mode:", json.load(urllib.request.urlopen(get, timeout=30)))
logout = urllib.request.Request(base + "/api/v1/auth/logout", b"{}",
                                {"Content-Type": "application/json", "Cookie": cookie})
urllib.request.urlopen(logout, timeout=30).read()
EOF
