from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import http.client
import ssl
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from typing import Any


_STANDARD_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}

_FORBIDDEN_FIELD_FRAGMENTS = {
    "api_hash",
    "authorization",
    "bot_token",
    "caption",
    "cookie",
    "credential",
    "local_path",
    "password",
    "private_key",
    "secret",
    "source_url",
    "token",
    "url",
}

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?hash|authorization|bot[_-]?token|cookie|password|passwd|secret|token)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BASIC_AUTH_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}")
_MANAGED_PATH_RE = re.compile(
    r"(?:(?:/app|/root/TGVIO)/(?:downloads|data|session|logs)/[^\s\"'<>]+)",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    value = _URL_RE.sub("<redacted-url>", value)
    value = _BASIC_AUTH_RE.sub("Basic <redacted>", value)
    value = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    value = _MANAGED_PATH_RE.sub("<redacted-path>", value)
    return value


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _field_allowed(name: str) -> bool:
    lowered = name.lower()
    return not any(fragment in lowered for fragment in _FORBIDDEN_FIELD_FRAGMENTS)


class SafeJsonFormatter(logging.Formatter):
    """One JSON object per line with aggressive secret/URL redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "component": record.name,
            "event": getattr(record, "event", "log.message"),
        }
        message = _redact_text(record.getMessage())
        if message:
            payload["message"] = message

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key in {"event", "message"}:
                continue
            if not _field_allowed(key):
                continue
            payload[key] = _safe_scalar(value)

        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["exception"] = _redact_text(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(
    *,
    level: str,
    log_dir: Path,
    file_enabled: bool,
    max_bytes: int,
    backup_count: int,
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = SafeJsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if file_enabled:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "tgvio.jsonl",
            maxBytes=max(1024 * 1024, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Keep useful connection lifecycle logs, but suppress noisy transport internals.
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str = "",
    *,
    exc_info: bool | BaseException | tuple[Any, Any, Any] | None = None,
    **fields: Any,
) -> None:
    safe_fields = {
        key: _safe_scalar(value)
        for key, value in fields.items()
        if _field_allowed(key) and value is not None
    }
    logger.log(
        level,
        _redact_text(message),
        extra={"event": event, **safe_fields},
        exc_info=exc_info,
    )


def maintenance_failure_fields(error: Exception) -> dict[str, Any]:
    """Use exact public transport messages as codes; never emit exception text."""
    codes = {
        "WebDAV collection creation failed": "archive_collection",
        "WebDAV metadata lookup failed": "archive_stat",
        "WebDAV archive PUT result was not verifiable": "archive_upload_unverified",
        "WebDAV archive PUT failed": "archive_upload_status",
        "WebDAV archive PUT failed size verification": "archive_upload_size",
        "WebDAV metadata PUT failed": "archive_metadata_put",
        "WebDAV metadata PUT failed size verification": "archive_metadata_size",
        "WebDAV metadata PUT failed content verification": "archive_metadata_content",
        "WebDAV metadata object exceeds read limit": "archive_metadata_budget",
        "archive metadata unavailable": "archive_metadata_read",
        "archive source unavailable": "archive_source_read",
        "archive source integrity mismatch": "archive_source_integrity",
        "media command failed": "media_command",
    }
    fields: dict[str, Any] = {"error": type(error).__name__,
                             "failure_code": codes.get(str(error), "unclassified")}
    cause = error.__cause__
    if type(cause) in (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                       BrokenPipeError, ConnectionRefusedError, ssl.SSLError,
                       http.client.RemoteDisconnected, http.client.IncompleteRead):
        fields["cause_error"] = type(cause).__name__
    status = getattr(error, "status", None)
    if type(status) is int and 100 <= status <= 599:
        fields["http_status"] = status
    return fields
