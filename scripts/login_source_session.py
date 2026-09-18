#!/usr/bin/env python3
"""One-time interactive login for the optional personal-account source session.

Run inside the container so the session lands in the mounted, gitignored
``session/`` volume:

    docker exec -it tgvio python /app/scripts/login_source_session.py

Telethon prompts for the phone number, the login code and (when enabled) the
two-step password. The resulting file grants full access to that account, so
treat it like a password: keep it at 0600 and never commit or copy it around.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from telethon import TelegramClient

sys.path.insert(0, "/app/src")
from tgvio.config import load_dotenv  # noqa: E402


def _session_path() -> str:
    return os.getenv("TGVIO_SOURCE_SESSION", "/app/session/source_user").strip() or (
        "/app/session/source_user"
    )


async def main() -> int:
    load_dotenv()
    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id or not api_hash:
        print("API_ID/API_HASH are missing from the environment", file=sys.stderr)
        return 2
    session = _session_path()
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, int(api_id), api_hash)
    await client.start()  # interactive: phone, code, optional 2FA password
    me = await client.get_me()
    await client.disconnect()
    target = Path(f"{session}.session")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print(f"source session ready: {target} (user_id={getattr(me, 'id', None)})")
    print("restart the bot container to start the source reader")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
