from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from tgvio.config import ConfigError, Settings


BASE_ENV = {
    "API_ID": "123456",
    "API_HASH": "hash-value",
    "BOT_TOKEN": "bot-token",
    "DEST_CHANNEL": "@destination",
    "ALLOWED_USERS": "42,43",
}


class SettingsTests(unittest.TestCase):
    def test_defaults_are_safe_for_parallel_rewrite(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.run_bot)
        self.assertFalse(settings.publish_enabled)
        self.assertFalse(settings.live_fixture_enabled)
        self.assertEqual(settings.live_fixture_max_bytes, 100 * 1024 * 1024)
        self.assertEqual(settings.vps_host, "199.47.242.40")
        self.assertEqual(settings.allowed_users, (42, 43))
        self.assertEqual(settings.channel_at, "@destination")
        self.assertEqual(settings.worker_concurrency, 2)
        self.assertEqual(settings.telegram_download_workers, 8)
        self.assertEqual(settings.telegram_upload_workers, 16)
        self.assertEqual(settings.telegram_upload_global_workers, 16)
        self.assertEqual(settings.telegram_part_size_kb, 512)
        self.assertEqual(settings.batch_window_ms, 1500)
        self.assertEqual(settings.batch_max_wait_ms, 5000)
        self.assertEqual(settings.batch_max_items, 100)
        self.assertTrue(settings.collections_enabled)
        self.assertEqual(settings.spoiler_confirm_timeout_seconds, 60)
        self.assertEqual(settings.disk_reserve_bytes, 5120 * 1024 * 1024)
        self.assertEqual(settings.upload_part_bytes, 1900 * 1024 * 1024)
        self.assertEqual(settings.cache_retention_hours, 24)
        self.assertEqual(settings.cache_cleanup_interval_minutes, 30)
        self.assertTrue(settings.auto_retry_enabled)
        self.assertEqual(settings.auto_retry_max_attempts, 3)
        self.assertEqual(settings.auto_retry_base_seconds, 15)
        self.assertEqual(settings.auto_retry_max_seconds, 300)
        self.assertEqual(settings.auto_retry_poll_seconds, 2)
        self.assertEqual(settings.log_level, "INFO")
        self.assertTrue(settings.log_file_enabled)
        self.assertEqual(settings.log_max_bytes, 20 * 1024 * 1024)
        self.assertEqual(settings.log_backup_count, 5)
        self.assertFalse(settings.url_enabled)
        self.assertEqual(settings.url_private_network_policy, "block")
        summary = settings.safe_summary()
        self.assertNotIn("bot-token", repr(summary))
        self.assertNotIn("hash-value", repr(summary))
        self.assertTrue(summary["log_file_enabled"])
        self.assertTrue(summary["auto_retry_enabled"])
        self.assertEqual(summary["auto_retry_max_attempts"], 3)

    def test_url_policy_is_explicit_and_validated(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_URL_ENABLED": "true",
                "TGVIO_URL_PRIVATE_NETWORK_POLICY": "warn",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.url_enabled)
        self.assertEqual(settings.url_private_network_policy, "warn")
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_URL_PRIVATE_NETWORK_POLICY": "maybe"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "URL_PRIVATE_NETWORK_POLICY"):
                Settings.from_env()

    def test_missing_secret_reports_name_not_value(self) -> None:
        env = dict(BASE_ENV)
        env.pop("BOT_TOKEN")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "BOT_TOKEN"):
                Settings.from_env()

    def test_vps_key_is_a_path_not_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {**BASE_ENV, "VPS_SSH_KEY": f"{tmp}/id_ed25519"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.vps_ssh_key.name, "id_ed25519")

    def test_explicit_channel_at_overrides_destination_footer(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "CHANNEL_AT": "@pretty_name"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.channel_at, "@pretty_name")

    def test_worker_and_disk_guard_ranges_are_validated(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_WORKER_CONCURRENCY": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "WORKER_CONCURRENCY"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_DISK_RESERVE_MB": "-1"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "DISK_RESERVE_MB"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_TELEGRAM_UPLOAD_WORKERS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "TELEGRAM_UPLOAD_WORKERS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_TELEGRAM_UPLOAD_GLOBAL_WORKERS": "33"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "TELEGRAM_UPLOAD_GLOBAL_WORKERS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_BATCH_WINDOW_MS": "10001"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "BATCH_WINDOW_MS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_BATCH_WINDOW_MS": "2000",
                "TGVIO_BATCH_MAX_WAIT_MS": "1000",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "BATCH_MAX_WAIT_MS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_BATCH_MAX_ITEMS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "BATCH_MAX_ITEMS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_SPOILER_CONFIRM_TIMEOUT_SECONDS": "4"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "SPOILER_CONFIRM_TIMEOUT_SECONDS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_CACHE_RETENTION_HOURS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "CACHE_RETENTION_HOURS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_CACHE_CLEANUP_INTERVAL_MINUTES": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "CACHE_CLEANUP_INTERVAL_MINUTES"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_AUTO_RETRY_MAX_ATTEMPTS": "11"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "AUTO_RETRY_MAX_ATTEMPTS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_AUTO_RETRY_BASE_SECONDS": "30",
                "TGVIO_AUTO_RETRY_MAX_SECONDS": "29",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "AUTO_RETRY_MAX_SECONDS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_AUTO_RETRY_POLL_SECONDS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "AUTO_RETRY_POLL_SECONDS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_LOG_LEVEL": "TRACE"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "TGVIO_LOG_LEVEL"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_LOG_MAX_MB": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "TGVIO_LOG_MAX_MB"):
                Settings.from_env()

    def test_archive_is_disabled_by_default_and_secrets_are_not_in_summary(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.archive_enabled)
        self.assertEqual(settings.archive_remote_root, "TGVIO")
        self.assertEqual(settings.archive_poll_seconds, 10)
        self.assertNotIn("archive_password", settings.safe_summary())

    def test_enabled_archive_requires_safe_absolute_webdav_url_and_user(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_ARCHIVE_ENABLED": "true",
                "TGVIO_ARCHIVE_WEBDAV_URL": "relative",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_WEBDAV_URL"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_ARCHIVE_ENABLED": "true",
                "TGVIO_ARCHIVE_WEBDAV_URL": "https://dav.example.test/root",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_WEBDAV_USER"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_ARCHIVE_ENABLED": "true",
                "TGVIO_ARCHIVE_WEBDAV_URL": "https://dav.example.test/root",
                "TGVIO_ARCHIVE_WEBDAV_USER": "alice",
                "TGVIO_ARCHIVE_WEBDAV_PASSWORD": "super-secret-password",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.archive_enabled)
        self.assertTrue(settings.safe_summary()["archive_configured"])
        self.assertNotIn("super-secret-password", repr(settings.safe_summary()))
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_UPLOAD_PART_MB": "1901"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "UPLOAD_PART_MB"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_LIVE_FIXTURE_MAX_MB": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "LIVE_FIXTURE_MAX_MB"):
                Settings.from_env()
