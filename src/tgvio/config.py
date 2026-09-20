from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import urllib.parse


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean environment variable: {name}")


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None and default is not None:
        return default
    if raw is None:
        raise ConfigError(f"missing required environment variable: {name}")
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"invalid integer environment variable: {name}") from exc


def _is_loopback_host(host: str) -> bool:
    candidate = (host or "").strip().lower()
    if candidate in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader so configuration does not depend on import-time magic."""

    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


@dataclass(frozen=True)
class Settings:
    environment: str
    run_bot: bool
    publish_enabled: bool
    live_fixture_enabled: bool
    live_fixture_max_bytes: int
    data_dir: Path
    download_dir: Path
    log_level: str
    log_dir: Path
    log_file_enabled: bool
    log_max_bytes: int
    log_backup_count: int
    api_id: int
    api_hash: str
    bot_token: str
    destination: str
    allowed_users: tuple[int, ...]
    channel_at: str
    group_at: str
    cover_mode: bool
    cover_width: int
    forward_caption: bool
    worker_concurrency: int
    telegram_download_workers: int
    telegram_upload_workers: int
    telegram_upload_global_workers: int
    telegram_part_size_kb: int
    telegram_shard_retries: int
    batch_window_ms: int
    batch_max_wait_ms: int
    batch_max_items: int
    collections_enabled: bool
    spoiler_confirm_timeout_seconds: int
    disk_reserve_bytes: int
    upload_part_bytes: int
    cache_retention_hours: int
    cache_cleanup_interval_minutes: int
    auto_retry_enabled: bool
    auto_retry_max_attempts: int
    auto_retry_base_seconds: int
    auto_retry_max_seconds: int
    auto_retry_poll_seconds: int
    url_enabled: bool
    url_private_network_policy: str
    ytdlp_cookies_file: str
    source_session: Path | None
    source_chats: tuple[str, ...]
    source_download_workers: int
    source_merge_max_items: int
    static_proxy_url: str = field(repr=False)
    static_proxy_probe_timeout_seconds: int
    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int
    dashboard_token: str = field(repr=False)
    webhook_enabled: bool
    webhook_url: str = field(repr=False)
    webhook_token: str = field(repr=False)
    webhook_timeout_seconds: int
    webhook_max_attempts: int
    notification_poll_seconds: int
    alerts_enabled: bool
    alert_user_id: int | None
    alert_cooldown_seconds: int
    alert_poll_seconds: int
    collection_preview_enabled: bool
    collection_editing_enabled: bool
    preview_enabled: bool
    preview_max_source_bytes: int
    preview_timeout_seconds: int
    archive_enabled: bool
    archive_url: str
    archive_remote_root: str
    archive_user: str
    archive_password: str
    archive_profile_id: str
    archive_policy: str
    archive_poll_seconds: int
    archive_response_timeout_seconds: int
    archive_verify_attempts: int
    archive_verify_interval_seconds: int
    archive_layout: str
    vps_host: str
    vps_port: int
    vps_user: str
    vps_ssh_key: Path
    vps_app_dir: str
    github_repo: str
    github_branch: str

    @classmethod
    def from_env(cls) -> "Settings":
        users_raw = _required("ALLOWED_USERS")
        try:
            users = tuple(int(item.strip()) for item in users_raw.split(",") if item.strip())
        except ValueError as exc:
            raise ConfigError("invalid ALLOWED_USERS") from exc
        if not users:
            raise ConfigError("ALLOWED_USERS must contain at least one user id")
        api_id = _int("API_ID")
        if api_id <= 0:
            raise ConfigError("API_ID must be positive")
        cover_width = _int("COVER_WIDTH", 1280)
        if not 128 <= cover_width <= 4096:
            raise ConfigError("COVER_WIDTH out of range")
        worker_concurrency = _int("TGVIO_WORKER_CONCURRENCY", 2)
        if not 1 <= worker_concurrency <= 16:
            raise ConfigError("TGVIO_WORKER_CONCURRENCY out of range")
        telegram_download_workers = _int("TGVIO_TELEGRAM_DOWNLOAD_WORKERS", 8)
        if not 1 <= telegram_download_workers <= 32:
            raise ConfigError("TGVIO_TELEGRAM_DOWNLOAD_WORKERS out of range")
        telegram_upload_workers = _int("TGVIO_TELEGRAM_UPLOAD_WORKERS", 16)
        if not 1 <= telegram_upload_workers <= 32:
            raise ConfigError("TGVIO_TELEGRAM_UPLOAD_WORKERS out of range")
        telegram_upload_global_workers = _int("TGVIO_TELEGRAM_UPLOAD_GLOBAL_WORKERS", 16)
        if not 1 <= telegram_upload_global_workers <= 32:
            raise ConfigError("TGVIO_TELEGRAM_UPLOAD_GLOBAL_WORKERS out of range")
        telegram_part_size_kb = _int("TGVIO_TELEGRAM_PART_SIZE_KB", 512)
        if telegram_part_size_kb not in {64, 128, 256, 512}:
            raise ConfigError("TGVIO_TELEGRAM_PART_SIZE_KB must be 64/128/256/512")
        telegram_shard_retries = _int("TGVIO_TELEGRAM_SHARD_RETRIES", 3)
        if not 0 <= telegram_shard_retries <= 10:
            raise ConfigError("TGVIO_TELEGRAM_SHARD_RETRIES out of range")
        batch_window_ms = _int("TGVIO_BATCH_WINDOW_MS", 1500)
        if not 0 <= batch_window_ms <= 10_000:
            raise ConfigError("TGVIO_BATCH_WINDOW_MS out of range")
        batch_max_wait_ms = _int("TGVIO_BATCH_MAX_WAIT_MS", 5000)
        if not max(1, batch_window_ms) <= batch_max_wait_ms <= 60_000:
            raise ConfigError("TGVIO_BATCH_MAX_WAIT_MS out of range")
        batch_max_items = _int("TGVIO_BATCH_MAX_ITEMS", 100)
        if not 1 <= batch_max_items <= 500:
            raise ConfigError("TGVIO_BATCH_MAX_ITEMS out of range")
        spoiler_confirm_timeout_seconds = _int("TGVIO_SPOILER_CONFIRM_TIMEOUT_SECONDS", 60)
        if not 5 <= spoiler_confirm_timeout_seconds <= 3600:
            raise ConfigError("TGVIO_SPOILER_CONFIRM_TIMEOUT_SECONDS out of range")
        disk_reserve_mb = _int("TGVIO_DISK_RESERVE_MB", 5120)
        if disk_reserve_mb < 0:
            raise ConfigError("TGVIO_DISK_RESERVE_MB must be >= 0")
        upload_part_mb = _int("TGVIO_UPLOAD_PART_MB", 1900)
        if not 16 <= upload_part_mb <= 1900:
            raise ConfigError("TGVIO_UPLOAD_PART_MB out of range")
        cache_retention_hours = _int("TGVIO_CACHE_RETENTION_HOURS", 24)
        if not 1 <= cache_retention_hours <= 24 * 90:
            raise ConfigError("TGVIO_CACHE_RETENTION_HOURS out of range")
        cache_cleanup_interval_minutes = _int("TGVIO_CACHE_CLEANUP_INTERVAL_MINUTES", 30)
        if not 1 <= cache_cleanup_interval_minutes <= 24 * 60:
            raise ConfigError("TGVIO_CACHE_CLEANUP_INTERVAL_MINUTES out of range")
        auto_retry_max_attempts = _int("TGVIO_AUTO_RETRY_MAX_ATTEMPTS", 3)
        if not 0 <= auto_retry_max_attempts <= 10:
            raise ConfigError("TGVIO_AUTO_RETRY_MAX_ATTEMPTS out of range")
        auto_retry_base_seconds = _int("TGVIO_AUTO_RETRY_BASE_SECONDS", 15)
        if not 1 <= auto_retry_base_seconds <= 3600:
            raise ConfigError("TGVIO_AUTO_RETRY_BASE_SECONDS out of range")
        auto_retry_max_seconds = _int("TGVIO_AUTO_RETRY_MAX_SECONDS", 300)
        if not auto_retry_base_seconds <= auto_retry_max_seconds <= 86400:
            raise ConfigError(
                "TGVIO_AUTO_RETRY_MAX_SECONDS must be >= base delay and <= 86400"
            )
        auto_retry_poll_seconds = _int("TGVIO_AUTO_RETRY_POLL_SECONDS", 2)
        if not 1 <= auto_retry_poll_seconds <= 300:
            raise ConfigError("TGVIO_AUTO_RETRY_POLL_SECONDS out of range")
        live_fixture_max_mb = _int("TGVIO_LIVE_FIXTURE_MAX_MB", 100)
        if not 1 <= live_fixture_max_mb <= 512:
            raise ConfigError("TGVIO_LIVE_FIXTURE_MAX_MB out of range")
        log_level = os.getenv("TGVIO_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("TGVIO_LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL")
        log_max_mb = _int("TGVIO_LOG_MAX_MB", 20)
        if not 1 <= log_max_mb <= 1024:
            raise ConfigError("TGVIO_LOG_MAX_MB out of range")
        log_backup_count = _int("TGVIO_LOG_BACKUP_COUNT", 5)
        if not 1 <= log_backup_count <= 100:
            raise ConfigError("TGVIO_LOG_BACKUP_COUNT out of range")
        destination = _required("DEST_CHANNEL")
        url_private_network_policy = os.getenv(
            "TGVIO_URL_PRIVATE_NETWORK_POLICY",
            "block",
        ).strip().lower() or "block"
        if url_private_network_policy not in {"allow", "warn", "block"}:
            raise ConfigError("TGVIO_URL_PRIVATE_NETWORK_POLICY must be allow/warn/block")
        static_proxy_url = os.getenv("TGVIO_STATIC_PROXY_URL", "").strip()
        static_proxy_probe_timeout_seconds = _int(
            "TGVIO_STATIC_PROXY_PROBE_TIMEOUT_SECONDS",
            2,
        )
        if not 1 <= static_proxy_probe_timeout_seconds <= 10:
            raise ConfigError("TGVIO_STATIC_PROXY_PROBE_TIMEOUT_SECONDS out of range")
        if static_proxy_url:
            try:
                parsed_proxy = urllib.parse.urlsplit(static_proxy_url)
                proxy_port = parsed_proxy.port
            except ValueError as exc:
                raise ConfigError("TGVIO_STATIC_PROXY_URL is malformed") from exc
            if parsed_proxy.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
                raise ConfigError("TGVIO_STATIC_PROXY_URL uses an unsupported scheme")
            if not parsed_proxy.hostname or parsed_proxy.query or parsed_proxy.fragment:
                raise ConfigError("TGVIO_STATIC_PROXY_URL must be an absolute proxy URL")
            if parsed_proxy.path not in {"", "/"}:
                raise ConfigError("TGVIO_STATIC_PROXY_URL must not contain a path")
            if proxy_port is not None and not 1 <= proxy_port <= 65535:
                raise ConfigError("TGVIO_STATIC_PROXY_URL has an invalid port")
        dashboard_enabled = _bool("TGVIO_DASHBOARD_ENABLED", False)
        dashboard_host = os.getenv("TGVIO_DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
        dashboard_port = _int("TGVIO_DASHBOARD_PORT", 8787)
        if not 1 <= dashboard_port <= 65535:
            raise ConfigError("TGVIO_DASHBOARD_PORT out of range")
        dashboard_token = os.getenv("TGVIO_DASHBOARD_TOKEN", "").strip()
        if dashboard_enabled:
            if not _is_loopback_host(dashboard_host):
                raise ConfigError("TGVIO_DASHBOARD_HOST must be a loopback address")
            if len(dashboard_token) < 32:
                raise ConfigError("TGVIO_DASHBOARD_TOKEN must be at least 32 characters")
        webhook_enabled = _bool("TGVIO_WEBHOOK_ENABLED", False)
        webhook_url = os.getenv("TGVIO_WEBHOOK_URL", "").strip()
        webhook_token = os.getenv("TGVIO_WEBHOOK_TOKEN", "")
        webhook_timeout_seconds = _int("TGVIO_WEBHOOK_TIMEOUT_SECONDS", 10)
        if not 1 <= webhook_timeout_seconds <= 60:
            raise ConfigError("TGVIO_WEBHOOK_TIMEOUT_SECONDS out of range")
        webhook_max_attempts = _int("TGVIO_WEBHOOK_MAX_ATTEMPTS", 5)
        if not 1 <= webhook_max_attempts <= 20:
            raise ConfigError("TGVIO_WEBHOOK_MAX_ATTEMPTS out of range")
        notification_poll_seconds = _int("TGVIO_NOTIFICATION_POLL_SECONDS", 15)
        if not 1 <= notification_poll_seconds <= 3600:
            raise ConfigError("TGVIO_NOTIFICATION_POLL_SECONDS out of range")
        alerts_enabled = _bool("TGVIO_ALERTS_ENABLED", True)
        alert_user_raw = os.getenv("TGVIO_ALERT_USER_ID", "").strip()
        alert_user_id: int | None = None
        if alert_user_raw:
            try:
                alert_user_id = int(alert_user_raw)
            except ValueError as exc:
                raise ConfigError("TGVIO_ALERT_USER_ID must be an integer") from exc
        alert_cooldown_seconds = _int("TGVIO_ALERT_COOLDOWN_SECONDS", 3600)
        if not 60 <= alert_cooldown_seconds <= 86400:
            raise ConfigError("TGVIO_ALERT_COOLDOWN_SECONDS out of range")
        alert_poll_seconds = _int("TGVIO_ALERT_POLL_SECONDS", 60)
        if not 5 <= alert_poll_seconds <= 3600:
            raise ConfigError("TGVIO_ALERT_POLL_SECONDS out of range")
        collection_preview_enabled = _bool("TGVIO_COLLECTION_PREVIEW_ENABLED", True)
        collection_editing_enabled = _bool("TGVIO_COLLECTION_EDITING_ENABLED", True)
        preview_enabled = _bool("TGVIO_PREVIEW_ENABLED", True)
        preview_max_source_bytes = _int("TGVIO_PREVIEW_MAX_SOURCE_BYTES", 33554432)
        if not 1 <= preview_max_source_bytes <= 2 * 1024**3:
            raise ConfigError("TGVIO_PREVIEW_MAX_SOURCE_BYTES out of range")
        preview_timeout_seconds = _int("TGVIO_PREVIEW_TIMEOUT_SECONDS", 30)
        if not 5 <= preview_timeout_seconds <= 600:
            raise ConfigError("TGVIO_PREVIEW_TIMEOUT_SECONDS out of range")
        if webhook_enabled:
            parsed_webhook = urllib.parse.urlsplit(webhook_url)
            if parsed_webhook.scheme != "https" or not parsed_webhook.hostname:
                raise ConfigError("TGVIO_WEBHOOK_URL must be an absolute HTTPS URL")
            if parsed_webhook.username or parsed_webhook.password:
                raise ConfigError("TGVIO_WEBHOOK_URL must not contain embedded credentials")
            if not webhook_token:
                raise ConfigError("TGVIO_WEBHOOK_TOKEN is required when the webhook is enabled")
        archive_enabled = _bool("TGVIO_ARCHIVE_ENABLED", False)
        archive_url = os.getenv("TGVIO_ARCHIVE_WEBDAV_URL", "").strip()
        archive_user = os.getenv("TGVIO_ARCHIVE_WEBDAV_USER", "").strip()
        archive_password = os.getenv("TGVIO_ARCHIVE_WEBDAV_PASSWORD", "")
        archive_remote_root = (
            os.getenv("TGVIO_ARCHIVE_REMOTE_ROOT", "TGVIO").strip().strip("/") or "TGVIO"
        )
        archive_profile_id = os.getenv("TGVIO_ARCHIVE_PROFILE_ID", "primary").strip() or "primary"
        if len(archive_profile_id) > 64 or any(
            not (character.isalnum() or character in {"-", "_", "."})
            for character in archive_profile_id
        ):
            raise ConfigError("TGVIO_ARCHIVE_PROFILE_ID contains unsafe characters")
        archive_policy = os.getenv("TGVIO_ARCHIVE_POLICY", "required").strip().lower() or "required"
        if archive_policy not in {"required", "best_effort"}:
            raise ConfigError("TGVIO_ARCHIVE_POLICY must be required/best_effort")
        if any(
            part in {".", ".."} or "\\" in part
            for part in archive_remote_root.split("/")
        ):
            raise ConfigError("TGVIO_ARCHIVE_REMOTE_ROOT contains unsafe path component")
        archive_poll_seconds = _int("TGVIO_ARCHIVE_POLL_SECONDS", 10)
        if not 1 <= archive_poll_seconds <= 300:
            raise ConfigError("TGVIO_ARCHIVE_POLL_SECONDS out of range")
        archive_response_timeout_seconds = _int("TGVIO_ARCHIVE_RESPONSE_TIMEOUT_SECONDS", 3600)
        if not 60 <= archive_response_timeout_seconds <= 86400:
            raise ConfigError("TGVIO_ARCHIVE_RESPONSE_TIMEOUT_SECONDS out of range")
        archive_verify_attempts = _int("TGVIO_ARCHIVE_VERIFY_ATTEMPTS", 120)
        if not 1 <= archive_verify_attempts <= 1000:
            raise ConfigError("TGVIO_ARCHIVE_VERIFY_ATTEMPTS out of range")
        archive_verify_interval_seconds = _int("TGVIO_ARCHIVE_VERIFY_INTERVAL_SECONDS", 20)
        if not 1 <= archive_verify_interval_seconds <= 600:
            raise ConfigError("TGVIO_ARCHIVE_VERIFY_INTERVAL_SECONDS out of range")
        archive_layout = os.getenv("TGVIO_ARCHIVE_LAYOUT", "v2").strip().lower() or "v2"
        if archive_layout not in {"v1", "v2"}:
            raise ConfigError("TGVIO_ARCHIVE_LAYOUT must be v1/v2")
        if archive_enabled:
            parsed = urllib.parse.urlsplit(archive_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ConfigError("TGVIO_ARCHIVE_WEBDAV_URL must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password:
                raise ConfigError("TGVIO_ARCHIVE_WEBDAV_URL must not contain embedded credentials")
            if not archive_user:
                raise ConfigError(
                    "TGVIO_ARCHIVE_WEBDAV_USER is required when archive is enabled"
                )
        channel_at = os.getenv("CHANNEL_AT", "").strip()
        if not channel_at and destination.startswith("@"):
            channel_at = destination
        source_session_raw = os.getenv(
            "TGVIO_SOURCE_SESSION", "/app/session/source_user"
        ).strip()
        source_session = Path(source_session_raw) if source_session_raw else None
        source_chats = tuple(
            part.strip()
            for part in os.getenv("TGVIO_SOURCE_CHATS", "").split(",")
            if part.strip()
        )
        source_download_workers = _int("TGVIO_SOURCE_DOWNLOAD_WORKERS", 4)
        if not 1 <= source_download_workers <= 16:
            raise ConfigError("TGVIO_SOURCE_DOWNLOAD_WORKERS out of range")
        source_merge_max_items = _int("TGVIO_MERGE_MAX_ITEMS", 100)
        if not 1 <= source_merge_max_items <= 2000:
            raise ConfigError("TGVIO_MERGE_MAX_ITEMS out of range")
        return cls(
            environment=os.getenv("TGVIO_ENV", "development").strip() or "development",
            run_bot=_bool("TGVIO_RUN_BOT", False),
            publish_enabled=_bool("TGVIO_PUBLISH_ENABLED", False),
            live_fixture_enabled=_bool("TGVIO_LIVE_FIXTURE_ENABLED", False),
            live_fixture_max_bytes=live_fixture_max_mb * 1024 * 1024,
            data_dir=Path(os.getenv("TGVIO_DATA_DIR", "/app/data")),
            download_dir=Path(os.getenv("TGVIO_DOWNLOAD_DIR", "/app/downloads")),
            log_level=log_level,
            log_dir=Path(os.getenv("TGVIO_LOG_DIR", "/app/logs")),
            log_file_enabled=_bool("TGVIO_LOG_FILE_ENABLED", True),
            log_max_bytes=log_max_mb * 1024 * 1024,
            log_backup_count=log_backup_count,
            api_id=api_id,
            api_hash=_required("API_HASH"),
            bot_token=_required("BOT_TOKEN"),
            destination=destination,
            allowed_users=users,
            channel_at=channel_at,
            group_at=os.getenv("GROUP_AT", "").strip(),
            cover_mode=_bool("COVER_MODE", True),
            cover_width=cover_width,
            forward_caption=_bool("FORWARD_CAPTION", False),
            worker_concurrency=worker_concurrency,
            telegram_download_workers=telegram_download_workers,
            telegram_upload_workers=telegram_upload_workers,
            telegram_upload_global_workers=telegram_upload_global_workers,
            telegram_part_size_kb=telegram_part_size_kb,
            telegram_shard_retries=telegram_shard_retries,
            batch_window_ms=batch_window_ms,
            batch_max_wait_ms=batch_max_wait_ms,
            batch_max_items=batch_max_items,
            collections_enabled=_bool("TGVIO_COLLECTIONS_ENABLED", True),
            spoiler_confirm_timeout_seconds=spoiler_confirm_timeout_seconds,
            disk_reserve_bytes=disk_reserve_mb * 1024 * 1024,
            upload_part_bytes=upload_part_mb * 1024 * 1024,
            cache_retention_hours=cache_retention_hours,
            cache_cleanup_interval_minutes=cache_cleanup_interval_minutes,
            auto_retry_enabled=_bool("TGVIO_AUTO_RETRY_ENABLED", True),
            auto_retry_max_attempts=auto_retry_max_attempts,
            auto_retry_base_seconds=auto_retry_base_seconds,
            auto_retry_max_seconds=auto_retry_max_seconds,
            auto_retry_poll_seconds=auto_retry_poll_seconds,
            url_enabled=_bool("TGVIO_URL_ENABLED", False),
            url_private_network_policy=url_private_network_policy,
            ytdlp_cookies_file=os.getenv("TGVIO_YTDLP_COOKIES_FILE", "").strip(),
            source_session=source_session,
            source_chats=source_chats,
            source_download_workers=source_download_workers,
            source_merge_max_items=source_merge_max_items,
            static_proxy_url=static_proxy_url,
            static_proxy_probe_timeout_seconds=static_proxy_probe_timeout_seconds,            dashboard_enabled=dashboard_enabled,
            dashboard_host=dashboard_host,
            dashboard_port=dashboard_port,
            dashboard_token=dashboard_token,
            webhook_enabled=webhook_enabled,
            webhook_url=webhook_url,
            webhook_token=webhook_token,
            webhook_timeout_seconds=webhook_timeout_seconds,
            webhook_max_attempts=webhook_max_attempts,
            notification_poll_seconds=notification_poll_seconds,
            alerts_enabled=alerts_enabled,
            alert_user_id=alert_user_id,
            alert_cooldown_seconds=alert_cooldown_seconds,
            alert_poll_seconds=alert_poll_seconds,
            collection_preview_enabled=collection_preview_enabled,
            collection_editing_enabled=collection_editing_enabled,
            preview_enabled=preview_enabled,
            preview_max_source_bytes=preview_max_source_bytes,
            preview_timeout_seconds=preview_timeout_seconds,
            archive_enabled=archive_enabled,
            archive_url=archive_url,
            archive_remote_root=archive_remote_root,
            archive_user=archive_user,
            archive_password=archive_password,
            archive_profile_id=archive_profile_id,
            archive_policy=archive_policy,
            archive_poll_seconds=archive_poll_seconds,
            archive_response_timeout_seconds=archive_response_timeout_seconds,
            archive_verify_attempts=archive_verify_attempts,
            archive_verify_interval_seconds=archive_verify_interval_seconds,
            archive_layout=archive_layout,
            vps_host=os.getenv("VPS_HOST", "199.47.242.40").strip(),
            vps_port=_int("VPS_PORT", 22),
            vps_user=os.getenv("VPS_USER", "root").strip() or "root",
            vps_ssh_key=Path(os.getenv("VPS_SSH_KEY", "/root/.ssh/id_ed25519")),
            vps_app_dir=os.getenv("VPS_APP_DIR", "/root/TGVIO").strip() or "/root/TGVIO",
            github_repo=os.getenv("GITHUB_REPO", "").strip(),
            github_branch=os.getenv("GITHUB_BRANCH", "main").strip() or "main",
        )

    def safe_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "run_bot": self.run_bot,
            "publish_enabled": self.publish_enabled,
            "live_fixture_enabled": self.live_fixture_enabled,
            "live_fixture_max_mb": self.live_fixture_max_bytes // (1024 * 1024),
            "allowed_users_count": len(self.allowed_users),
            "destination_kind": "username" if self.destination.startswith("@") else "id",
            "cover_mode": self.cover_mode,
            "forward_caption": self.forward_caption,
            "worker_concurrency": self.worker_concurrency,
            "telegram_download_workers": self.telegram_download_workers,
            "telegram_upload_workers": self.telegram_upload_workers,
            "telegram_upload_global_workers": self.telegram_upload_global_workers,
            "telegram_part_size_kb": self.telegram_part_size_kb,
            "telegram_shard_retries": self.telegram_shard_retries,
            "batch_window_ms": self.batch_window_ms,
            "batch_max_items": self.batch_max_items,
            "collections_enabled": self.collections_enabled,
            "spoiler_confirm_timeout_seconds": self.spoiler_confirm_timeout_seconds,
            "disk_reserve_mb": self.disk_reserve_bytes // (1024 * 1024),
            "upload_part_mb": self.upload_part_bytes // (1024 * 1024),
            "cache_retention_hours": self.cache_retention_hours,
            "auto_retry_enabled": self.auto_retry_enabled,
            "auto_retry_max_attempts": self.auto_retry_max_attempts,
            "auto_retry_base_seconds": self.auto_retry_base_seconds,
            "auto_retry_max_seconds": self.auto_retry_max_seconds,
            "log_level": self.log_level,
            "log_file_enabled": self.log_file_enabled,
            "log_max_mb": self.log_max_bytes // (1024 * 1024),
            "log_backup_count": self.log_backup_count,
            "url_enabled": self.url_enabled,
            "url_private_network_policy": self.url_private_network_policy,
            "ytdlp_cookies_configured": bool(self.ytdlp_cookies_file),
            "source_session_configured": bool(self.source_session),
            "source_chat_count": len(self.source_chats),
            "source_download_workers": self.source_download_workers,
            "source_merge_max_items": self.source_merge_max_items,
            "static_proxy_configured": bool(self.static_proxy_url),
            "static_proxy_probe_timeout_seconds": self.static_proxy_probe_timeout_seconds,
            "dashboard_enabled": self.dashboard_enabled,
            "dashboard_host_class": "loopback" if _is_loopback_host(self.dashboard_host) else "other",
            "dashboard_port": self.dashboard_port,
            "webhook_enabled": self.webhook_enabled,
            "notification_poll_seconds": self.notification_poll_seconds,
            "alerts_enabled": self.alerts_enabled,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
            "alert_poll_seconds": self.alert_poll_seconds,
            "collection_preview_enabled": self.collection_preview_enabled,
            "collection_editing_enabled": self.collection_editing_enabled,
            "preview_enabled": self.preview_enabled,
            "archive_enabled": self.archive_enabled,
            "archive_configured": bool(self.archive_url and self.archive_user),
            "archive_remote_root_configured": bool(self.archive_remote_root),
            "archive_profile_id": self.archive_profile_id,
            "archive_policy": self.archive_policy,
            "archive_response_timeout_seconds": self.archive_response_timeout_seconds,
            "archive_verify_attempts": self.archive_verify_attempts,
            "archive_verify_interval_seconds": self.archive_verify_interval_seconds,
            "archive_layout": self.archive_layout,
            "vps_host_configured": bool(self.vps_host),
            "vps_ssh_key_configured": bool(str(self.vps_ssh_key)),
            "github_repo_configured": bool(self.github_repo),
        }
