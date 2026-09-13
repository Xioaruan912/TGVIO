from __future__ import annotations

from typing import Any

from tgvio.application.ports import JobRepository


DEFAULT_FLAGS: dict[str, str] = {
    "alerts_enabled": "true",
    "collection_preview_enabled": "true",
    "daily_cleanup_enabled": "true",
    "daily_cleanup_time": "06:00",
}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class RuntimeFlags:
    """Small in-memory cache of durable runtime feature flags.

    Flags are persisted in SQLite so `/settings` toggles survive restarts and
    take effect without editing the deployment `.env`.
    """

    def __init__(self, *, overrides: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(DEFAULT_FLAGS)
        if overrides:
            self._values.update({str(k): str(v) for k, v in overrides.items()})

    def get(self, key: str, default: Any = None) -> str | None:
        return self._values.get(key, DEFAULT_FLAGS.get(key, default))

    def bool(self, key: str, default: bool = False) -> bool:
        return _as_bool(self._values.get(key), default)

    def snapshot(self) -> dict[str, str]:
        return dict(self._values)

    async def load(self, repository: JobRepository) -> None:
        try:
            stored = await repository.get_runtime_flags()
        except Exception:
            stored = {}
        self._values = dict(DEFAULT_FLAGS)
        self._values.update({str(k): str(v) for k, v in (stored or {}).items()})

    async def set(self, repository: JobRepository, key: str, value: str) -> None:
        self._values[str(key)] = str(value)
        await repository.set_runtime_flag(str(key), str(value))
