import os

from dotenv import load_dotenv

load_dotenv()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


API_ID = _env_int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEST_CHANNEL = os.environ.get("DEST_CHANNEL", "")

ALLOWED_USERS = {
    int(x.strip()) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}

MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE", 2000 * 1024 * 1024)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
DOWNLOAD_CONCURRENCY = _env_int("DOWNLOAD_CONCURRENCY", 3)
DOWNLOAD_TIMEOUT = _env_int("DOWNLOAD_TIMEOUT", 20 * 60)
CONFIRM_TIMEOUT = _env_int("CONFIRM_TIMEOUT", 60)
ALBUM_GATHER_SECONDS = _env_float("ALBUM_GATHER_SECONDS", 1.0)
UPLOAD_TIMEOUT = _env_int("UPLOAD_TIMEOUT", 30 * 60)
FORWARD_CAPTION = _env_bool("FORWARD_CAPTION", False)
PROGRESS_MIN_INTERVAL = _env_float("PROGRESS_MIN_INTERVAL", 2.0)
HELD_TIMEOUT = _env_int("HELD_TIMEOUT", 300)
AUTO_DELETE_SECONDS = _env_int("AUTO_DELETE_SECONDS", 10)
DOWNLOAD_AUTO_RETRY = _env_int("DOWNLOAD_AUTO_RETRY", 1)
