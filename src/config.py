"""Validated application configuration.

Static process configuration is represented by immutable :class:`Settings`.
Runtime-editable WebDAV settings remain separate and are only exposed here as
a legacy bootstrap during the migration away from module-level constants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv


load_dotenv()


class SettingsError(ValueError):
    """Configuration is missing or outside the supported range."""


def _raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value if value is not None else None


def _int(env: Mapping[str, str], key: str, default: int, *, strict: bool) -> int:
    value = _raw(env, key)
    if value is None or value.strip() == "":
        return int(default)
    try:
        return int(value)
    except ValueError as exc:
        if strict:
            raise SettingsError(f"invalid configuration: {key}") from exc
        return int(default)


def _float(env: Mapping[str, str], key: str, default: float, *, strict: bool) -> float:
    value = _raw(env, key)
    if value is None or value.strip() == "":
        return float(default)
    try:
        return float(value)
    except ValueError as exc:
        if strict:
            raise SettingsError(f"invalid configuration: {key}") from exc
        return float(default)


def _bool(env: Mapping[str, str], key: str, default: bool, *, strict: bool) -> bool:
    value = _raw(env, key)
    if value is None or value.strip() == "":
        return bool(default)
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if strict:
        raise SettingsError(f"invalid configuration: {key}")
    return bool(default)


def _bounded(key: str, value: int | float, low: int | float, high: int | float) -> None:
    if value < low or value > high:
        raise SettingsError(f"invalid configuration range: {key}")


def _strong_secret(key: str, value: str) -> None:
    encoded = str(value).encode("utf-8")
    if len(encoded) < 32 or len(encoded) > 512 or any(byte < 33 or byte > 126 for byte in encoded):
        raise SettingsError(f"invalid configuration: {key}")


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    dest_channel: str
    allowed_users: frozenset[int]
    channel_at: str
    group_at: str
    max_file_size: int
    large_file_policy: str
    split_part_bytes: int
    download_dir: str
    download_concurrency: int
    download_timeout: int
    confirm_timeout: int
    collection_gather_seconds: float
    upload_timeout: int
    forward_caption: bool
    progress_min_interval: float
    auto_delete_seconds: int
    download_auto_retry: int
    download_workers: int
    upload_workers: int
    part_size_kb: int
    cover_mode: bool
    cover_width: int
    max_cover_images: int
    session_collect: bool
    session_end_timeout: float
    disk_enforce: bool
    min_free_bytes: int
    min_free_percent: float
    max_cache_bytes: int
    cache_retention_hours: float
    failed_cache_retention_hours: float
    history_retention_days: int
    event_retention_days: int
    disk_check_interval: float
    unknown_job_reserve_bytes: int
    health_heartbeat_max_age: int
    health_min_free_bytes: int
    media_compat_mode: str
    faststart_max_bytes: int
    transcode_enabled: bool
    thumbnail_position: str
    url_private_network_policy: str
    dashboard_enabled: bool
    dashboard_public_bind: bool
    dashboard_host: str
    dashboard_port: int
    dashboard_socket: str
    dashboard_token: str
    webhook_enabled: bool
    webhook_url: str
    webhook_token: str
    webhook_timeout: float
    webhook_max_attempts: int
    webhook_poll_interval: float

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool = True,
    ) -> "Settings":
        env = os.environ if environ is None else environ
        required = ("API_ID", "API_HASH", "BOT_TOKEN", "DEST_CHANNEL", "ALLOWED_USERS")
        if strict:
            missing = [key for key in required if not str(env.get(key, "")).strip()]
            if missing:
                raise SettingsError("missing required environment variables: " + ", ".join(missing))

        api_id = _int(env, "API_ID", 0, strict=strict)
        api_hash = str(env.get("API_HASH", "")).strip()
        bot_token = str(env.get("BOT_TOKEN", "")).strip()
        dest_channel = str(env.get("DEST_CHANNEL", "")).strip()
        raw_users = str(env.get("ALLOWED_USERS", ""))
        try:
            allowed_users = frozenset(
                int(value.strip()) for value in raw_users.split(",") if value.strip()
            )
        except ValueError as exc:
            if strict:
                raise SettingsError("invalid configuration: ALLOWED_USERS") from exc
            allowed_users = frozenset()
        if strict and (api_id <= 0 or not allowed_users):
            key = "API_ID" if api_id <= 0 else "ALLOWED_USERS"
            raise SettingsError(f"invalid configuration: {key}")

        channel_at = str(env.get("CHANNEL_AT", "")).strip()
        if not channel_at and dest_channel.startswith("@"):
            channel_at = dest_channel
        media_mode = str(env.get("MEDIA_COMPAT_MODE", "analyze")).strip().lower() or "analyze"
        if media_mode not in {"off", "analyze", "remux"}:
            if strict:
                raise SettingsError("invalid configuration: MEDIA_COMPAT_MODE")
            media_mode = "analyze"
        thumbnail_position = str(env.get("THUMBNAIL_POSITION", "auto")).strip().lower() or "auto"
        if thumbnail_position != "auto":
            try:
                if float(thumbnail_position) < 0:
                    raise ValueError
            except ValueError as exc:
                if strict:
                    raise SettingsError("invalid configuration: THUMBNAIL_POSITION") from exc
                thumbnail_position = "auto"
        url_private_network_policy = str(
            env.get("URL_PRIVATE_NETWORK_POLICY", "warn")
        ).strip().lower() or "warn"
        if url_private_network_policy not in {"allow", "warn", "block"}:
            if strict:
                raise SettingsError("invalid configuration: URL_PRIVATE_NETWORK_POLICY")
            url_private_network_policy = "warn"

        max_file_size = _int(env, "MAX_FILE_SIZE", 2000 * 1024 * 1024, strict=strict)
        split_default = min(1900 * 1024 * 1024, max(1, max_file_size - 16 * 1024 * 1024))
        settings = cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            dest_channel=dest_channel,
            allowed_users=allowed_users,
            channel_at=channel_at,
            group_at=str(env.get("GROUP_AT", "")).strip(),
            max_file_size=max_file_size,
            large_file_policy=str(env.get("LARGE_FILE_POLICY", "reject")).strip().lower() or "reject",
            split_part_bytes=_int(env, "SPLIT_PART_BYTES", split_default, strict=strict),
            download_dir=str(env.get("DOWNLOAD_DIR", "/app/downloads")).strip() or "/app/downloads",
            download_concurrency=_int(env, "DOWNLOAD_CONCURRENCY", 3, strict=strict),
            download_timeout=_int(env, "DOWNLOAD_TIMEOUT", 20 * 60, strict=strict),
            confirm_timeout=_int(env, "CONFIRM_TIMEOUT", 60, strict=strict),
            collection_gather_seconds=_float(env, "COLLECTION_GATHER_SECONDS", 10.0, strict=strict),
            upload_timeout=_int(env, "UPLOAD_TIMEOUT", 30 * 60, strict=strict),
            forward_caption=_bool(env, "FORWARD_CAPTION", False, strict=strict),
            progress_min_interval=_float(env, "PROGRESS_MIN_INTERVAL", 2.0, strict=strict),
            auto_delete_seconds=_int(env, "AUTO_DELETE_SECONDS", 10, strict=strict),
            download_auto_retry=_int(env, "DOWNLOAD_AUTO_RETRY", 2, strict=strict),
            download_workers=_int(env, "DOWNLOAD_WORKERS", 8, strict=strict),
            upload_workers=_int(env, "UPLOAD_WORKERS", 16, strict=strict),
            part_size_kb=_int(env, "PART_SIZE_KB", 512, strict=strict),
            cover_mode=_bool(env, "COVER_MODE", False, strict=strict),
            cover_width=_int(env, "COVER_WIDTH", 1280, strict=strict),
            max_cover_images=_int(env, "MAX_COVER_IMAGES", 10, strict=strict),
            session_collect=_bool(env, "SESSION_COLLECT", True, strict=strict),
            session_end_timeout=_float(env, "SESSION_END_TIMEOUT", 5.0, strict=strict),
            disk_enforce=_bool(env, "DISK_ENFORCE", False, strict=strict),
            min_free_bytes=_int(env, "MIN_FREE_BYTES", 5 * 1024**3, strict=strict),
            min_free_percent=_float(env, "MIN_FREE_PERCENT", 10.0, strict=strict),
            max_cache_bytes=_int(env, "MAX_CACHE_BYTES", 0, strict=strict),
            cache_retention_hours=_float(env, "CACHE_RETENTION_HOURS", 72.0, strict=strict),
            failed_cache_retention_hours=_float(env, "FAILED_CACHE_RETENTION_HOURS", 168.0, strict=strict),
            history_retention_days=_int(env, "HISTORY_RETENTION_DAYS", 30, strict=strict),
            event_retention_days=_int(env, "EVENT_RETENTION_DAYS", 30, strict=strict),
            disk_check_interval=_float(env, "DISK_CHECK_INTERVAL", 60.0, strict=strict),
            unknown_job_reserve_bytes=_int(env, "UNKNOWN_JOB_RESERVE_BYTES", 2 * 1024**3, strict=strict),
            health_heartbeat_max_age=_int(env, "HEALTH_HEARTBEAT_MAX_AGE", 45, strict=strict),
            health_min_free_bytes=_int(env, "HEALTH_MIN_FREE_BYTES", 256 * 1024**2, strict=strict),
            media_compat_mode=media_mode,
            faststart_max_bytes=_int(env, "FASTSTART_MAX_BYTES", 0, strict=strict),
            transcode_enabled=_bool(env, "TRANSCODE_ENABLED", False, strict=strict),
            thumbnail_position=thumbnail_position,
            url_private_network_policy=url_private_network_policy,
            dashboard_enabled=_bool(env, "DASHBOARD_ENABLED", False, strict=strict),
            dashboard_public_bind=_bool(env, "DASHBOARD_PUBLIC_BIND", False, strict=strict),
            dashboard_host=str(env.get("DASHBOARD_HOST", "127.0.0.1")).strip() or "127.0.0.1",
            dashboard_port=_int(env, "DASHBOARD_PORT", 8787, strict=strict),
            dashboard_socket=str(env.get("DASHBOARD_SOCKET", "session/dashboard.sock")).strip(),
            dashboard_token=str(env.get("DASHBOARD_TOKEN", "")).strip(),
            webhook_enabled=_bool(env, "WEBHOOK_ENABLED", False, strict=strict),
            webhook_url=str(env.get("WEBHOOK_URL", "")).strip(),
            webhook_token=str(env.get("WEBHOOK_TOKEN", "")).strip(),
            webhook_timeout=_float(env, "WEBHOOK_TIMEOUT", 10.0, strict=strict),
            webhook_max_attempts=_int(env, "WEBHOOK_MAX_ATTEMPTS", 8, strict=strict),
            webhook_poll_interval=_float(env, "WEBHOOK_POLL_INTERVAL", 2.0, strict=strict),
        )
        if strict:
            settings.validate()
        return settings

    def validate(self) -> None:
        _bounded("MAX_FILE_SIZE", self.max_file_size, 1, 4 * 1024**3)
        if self.large_file_policy not in {"reject", "split"}:
            raise SettingsError("invalid configuration: LARGE_FILE_POLICY")
        if self.large_file_policy == "split":
            _bounded("SPLIT_PART_BYTES", self.split_part_bytes, 64 * 1024**2, self.max_file_size - 1)
        _bounded("DOWNLOAD_CONCURRENCY", self.download_concurrency, 1, 32)
        _bounded("DOWNLOAD_TIMEOUT", self.download_timeout, 30, 24 * 3600)
        _bounded("CONFIRM_TIMEOUT", self.confirm_timeout, 5, 3600)
        _bounded("COLLECTION_GATHER_SECONDS", self.collection_gather_seconds, 0.5, 120.0)
        _bounded("UPLOAD_TIMEOUT", self.upload_timeout, 30, 24 * 3600)
        _bounded("PROGRESS_MIN_INTERVAL", self.progress_min_interval, 0.2, 60.0)
        _bounded("AUTO_DELETE_SECONDS", self.auto_delete_seconds, 0, 86400)
        _bounded("DOWNLOAD_AUTO_RETRY", self.download_auto_retry, 0, 20)
        _bounded("DOWNLOAD_WORKERS", self.download_workers, 1, 64)
        _bounded("UPLOAD_WORKERS", self.upload_workers, 1, 64)
        if self.part_size_kb not in {64, 128, 256, 512}:
            raise SettingsError("invalid configuration: PART_SIZE_KB")
        _bounded("COVER_WIDTH", self.cover_width, 64, 4096)
        _bounded("MAX_COVER_IMAGES", self.max_cover_images, 1, 10)
        _bounded("SESSION_END_TIMEOUT", self.session_end_timeout, 0.2, 300.0)
        _bounded("MIN_FREE_BYTES", self.min_free_bytes, 0, 1024**5)
        _bounded("MIN_FREE_PERCENT", self.min_free_percent, 0.0, 100.0)
        _bounded("MAX_CACHE_BYTES", self.max_cache_bytes, 0, 1024**5)
        _bounded("CACHE_RETENTION_HOURS", self.cache_retention_hours, 0.0, 24 * 365.0)
        _bounded("FAILED_CACHE_RETENTION_HOURS", self.failed_cache_retention_hours, 0.0, 24 * 365.0)
        _bounded("HISTORY_RETENTION_DAYS", self.history_retention_days, 1, 3650)
        _bounded("EVENT_RETENTION_DAYS", self.event_retention_days, 30, 90)
        _bounded("DISK_CHECK_INTERVAL", self.disk_check_interval, 1.0, 3600.0)
        _bounded("UNKNOWN_JOB_RESERVE_BYTES", self.unknown_job_reserve_bytes, 0, 1024**5)
        _bounded("HEALTH_HEARTBEAT_MAX_AGE", self.health_heartbeat_max_age, 5, 3600)
        _bounded("HEALTH_MIN_FREE_BYTES", self.health_min_free_bytes, 0, 1024**5)
        _bounded("FASTSTART_MAX_BYTES", self.faststart_max_bytes, 0, 4 * 1024**3)
        _bounded("DASHBOARD_PORT", self.dashboard_port, 1024, 65535)
        _bounded("WEBHOOK_TIMEOUT", self.webhook_timeout, 1.0, 60.0)
        _bounded("WEBHOOK_MAX_ATTEMPTS", self.webhook_max_attempts, 1, 20)
        _bounded("WEBHOOK_POLL_INTERVAL", self.webhook_poll_interval, 0.2, 60.0)
        if self.dashboard_enabled:
            allowed_hosts = {"127.0.0.1", "::1", "localhost"}
            if self.dashboard_public_bind:
                allowed_hosts |= {"0.0.0.0", "::"}
                if self.dashboard_socket:
                    raise SettingsError("invalid configuration: DASHBOARD_SOCKET")
            if self.dashboard_host not in allowed_hosts:
                raise SettingsError("invalid configuration: DASHBOARD_HOST")
            if not self.dashboard_socket and self.dashboard_host == "localhost":
                # Avoid DNS-dependent bind behavior; TCP listeners use a literal loopback.
                raise SettingsError("invalid configuration: DASHBOARD_HOST")
            if "\x00" in self.dashboard_socket:
                raise SettingsError("invalid configuration: DASHBOARD_SOCKET")
            _strong_secret("DASHBOARD_TOKEN", self.dashboard_token)
        if self.webhook_enabled:
            try:
                webhook = urlsplit(self.webhook_url)
                webhook_port = webhook.port
            except ValueError as exc:
                raise SettingsError("invalid configuration: WEBHOOK_URL") from exc
            if (
                webhook.scheme.lower() != "https"
                or not webhook.hostname
                or webhook.username is not None
                or webhook.password is not None
                or webhook.fragment
                or webhook_port == 0
            ):
                raise SettingsError("invalid configuration: WEBHOOK_URL")
            _strong_secret("WEBHOOK_TOKEN", self.webhook_token)

    def safe_summary(self) -> dict[str, object]:
        """Return diagnostic-safe static configuration without identities/secrets."""
        return {
            "destination_kind": (
                "username" if self.dest_channel.startswith("@") else "numeric_or_other"
            ),
            "allowed_users_count": len(self.allowed_users),
            "download_concurrency": self.download_concurrency,
            "download_workers": self.download_workers,
            "upload_workers": self.upload_workers,
            "part_size_kb": self.part_size_kb,
            "cover_mode": self.cover_mode,
            "forward_caption": self.forward_caption,
            "session_collect": self.session_collect,
            "disk_enforce": self.disk_enforce,
            "history_retention_days": self.history_retention_days,
            "event_retention_days": self.event_retention_days,
            "media_compat_mode": self.media_compat_mode,
            "transcode_enabled": self.transcode_enabled,
            "url_private_network_policy": self.url_private_network_policy,
            "download_dir_configured": bool(self.download_dir),
            "dashboard_enabled": self.dashboard_enabled,
            "dashboard_transport": "unix" if self.dashboard_socket else ("tcp-public" if self.dashboard_public_bind else "tcp-loopback"),
            "dashboard_port": self.dashboard_port,
            "webhook_enabled": self.webhook_enabled,
            "webhook_max_attempts": self.webhook_max_attempts,
        }


@dataclass(frozen=True)
class LegacyWebDavBootstrap:
    enabled: bool
    url: str
    user: str
    password: str
    path: str
    retry: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LegacyWebDavBootstrap":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_bool(env, "WEBDAV_ENABLED", False, strict=False),
            url=str(env.get("WEBDAV_URL", "")),
            user=str(env.get("WEBDAV_USER", "")),
            password=str(env.get("WEBDAV_PASS", "")),
            path=str(env.get("WEBDAV_PATH", "/115/Pron")),
            retry=_int(env, "WEBDAV_RETRY", 5, strict=False),
        )


# One-cycle compatibility exports. Runtime startup uses strict Settings.from_env().
SETTINGS = Settings.from_env(strict=False)
WEBDAV_BOOTSTRAP = LegacyWebDavBootstrap.from_env()

API_ID = SETTINGS.api_id
API_HASH = SETTINGS.api_hash
BOT_TOKEN = SETTINGS.bot_token
DEST_CHANNEL = SETTINGS.dest_channel
ALLOWED_USERS = set(SETTINGS.allowed_users)
CHANNEL_AT = SETTINGS.channel_at
GROUP_AT = SETTINGS.group_at
MAX_FILE_SIZE = SETTINGS.max_file_size
DOWNLOAD_DIR = SETTINGS.download_dir
DOWNLOAD_CONCURRENCY = SETTINGS.download_concurrency
DOWNLOAD_TIMEOUT = SETTINGS.download_timeout
CONFIRM_TIMEOUT = SETTINGS.confirm_timeout
COLLECTION_GATHER_SECONDS = SETTINGS.collection_gather_seconds
UPLOAD_TIMEOUT = SETTINGS.upload_timeout
FORWARD_CAPTION = SETTINGS.forward_caption
PROGRESS_MIN_INTERVAL = SETTINGS.progress_min_interval
AUTO_DELETE_SECONDS = SETTINGS.auto_delete_seconds
DOWNLOAD_AUTO_RETRY = SETTINGS.download_auto_retry
DOWNLOAD_WORKERS = SETTINGS.download_workers
UPLOAD_WORKERS = SETTINGS.upload_workers
PART_SIZE_KB = SETTINGS.part_size_kb
COVER_MODE = SETTINGS.cover_mode
COVER_WIDTH = SETTINGS.cover_width
MAX_COVER_IMAGES = SETTINGS.max_cover_images
SESSION_COLLECT = SETTINGS.session_collect
SESSION_END_TIMEOUT = SETTINGS.session_end_timeout
DISK_ENFORCE = SETTINGS.disk_enforce
MIN_FREE_BYTES = SETTINGS.min_free_bytes
MIN_FREE_PERCENT = SETTINGS.min_free_percent
MAX_CACHE_BYTES = SETTINGS.max_cache_bytes
CACHE_RETENTION_HOURS = SETTINGS.cache_retention_hours
FAILED_CACHE_RETENTION_HOURS = SETTINGS.failed_cache_retention_hours
HISTORY_RETENTION_DAYS = SETTINGS.history_retention_days
EVENT_RETENTION_DAYS = SETTINGS.event_retention_days
DISK_CHECK_INTERVAL = SETTINGS.disk_check_interval
UNKNOWN_JOB_RESERVE_BYTES = SETTINGS.unknown_job_reserve_bytes
HEALTH_HEARTBEAT_MAX_AGE = SETTINGS.health_heartbeat_max_age
HEALTH_MIN_FREE_BYTES = SETTINGS.health_min_free_bytes
MEDIA_COMPAT_MODE = SETTINGS.media_compat_mode
FASTSTART_MAX_BYTES = SETTINGS.faststart_max_bytes
TRANSCODE_ENABLED = SETTINGS.transcode_enabled
THUMBNAIL_POSITION = SETTINGS.thumbnail_position
URL_PRIVATE_NETWORK_POLICY = SETTINGS.url_private_network_policy
DASHBOARD_ENABLED = SETTINGS.dashboard_enabled
DASHBOARD_HOST = SETTINGS.dashboard_host
DASHBOARD_PORT = SETTINGS.dashboard_port
DASHBOARD_SOCKET = SETTINGS.dashboard_socket
WEBHOOK_ENABLED = SETTINGS.webhook_enabled
WEBHOOK_TIMEOUT = SETTINGS.webhook_timeout
WEBHOOK_MAX_ATTEMPTS = SETTINGS.webhook_max_attempts
WEBHOOK_POLL_INTERVAL = SETTINGS.webhook_poll_interval

WEBDAV_ENABLED = WEBDAV_BOOTSTRAP.enabled
WEBDAV_URL = WEBDAV_BOOTSTRAP.url
WEBDAV_USER = WEBDAV_BOOTSTRAP.user
WEBDAV_PASS = WEBDAV_BOOTSTRAP.password
WEBDAV_PATH = WEBDAV_BOOTSTRAP.path
WEBDAV_RETRY = WEBDAV_BOOTSTRAP.retry
