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

CHANNEL_AT = (
    os.environ.get("CHANNEL_AT", "").strip()
    or (DEST_CHANNEL if DEST_CHANNEL.startswith("@") else "")
)
GROUP_AT = os.environ.get("GROUP_AT", "").strip()

MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE", 2000 * 1024 * 1024)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
DOWNLOAD_CONCURRENCY = _env_int("DOWNLOAD_CONCURRENCY", 3)
DOWNLOAD_TIMEOUT = _env_int("DOWNLOAD_TIMEOUT", 20 * 60)
CONFIRM_TIMEOUT = _env_int("CONFIRM_TIMEOUT", 60)
COLLECTION_GATHER_SECONDS = _env_float("COLLECTION_GATHER_SECONDS", 10.0)
UPLOAD_TIMEOUT = _env_int("UPLOAD_TIMEOUT", 30 * 60)
FORWARD_CAPTION = _env_bool("FORWARD_CAPTION", False)
PROGRESS_MIN_INTERVAL = _env_float("PROGRESS_MIN_INTERVAL", 2.0)
AUTO_DELETE_SECONDS = _env_int("AUTO_DELETE_SECONDS", 10)
DOWNLOAD_AUTO_RETRY = _env_int("DOWNLOAD_AUTO_RETRY", 1)
DOWNLOAD_WORKERS = _env_int("DOWNLOAD_WORKERS", 8)
UPLOAD_WORKERS = _env_int("UPLOAD_WORKERS", 16)
PART_SIZE_KB = _env_int("PART_SIZE_KB", 512)
COVER_MODE = _env_bool("COVER_MODE", False)
COVER_WIDTH = _env_int("COVER_WIDTH", 1280)
MAX_COVER_IMAGES = _env_int("MAX_COVER_IMAGES", 10)
SESSION_COLLECT = _env_bool("SESSION_COLLECT", True)
SESSION_END_TIMEOUT = _env_float("SESSION_END_TIMEOUT", 5.0)

WEBDAV_ENABLED = _env_bool("WEBDAV_ENABLED", False)
WEBDAV_URL = os.environ.get("WEBDAV_URL", "")
WEBDAV_USER = os.environ.get("WEBDAV_USER", "")
WEBDAV_PASS = os.environ.get("WEBDAV_PASS", "")
WEBDAV_PATH = os.environ.get("WEBDAV_PATH", "/影视相关/Pron")
WEBDAV_RETRY = _env_int("WEBDAV_RETRY", 2)
