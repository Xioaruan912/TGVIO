from __future__ import annotations

import os
from pathlib import Path
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
        self.assertEqual(settings.ytdlp_cookies_file, "")
        self.assertEqual(settings.source_session, Path("/app/session/source_user"))
        self.assertEqual(settings.source_chats, ())
        self.assertEqual(settings.source_download_workers, 4)
        self.assertTrue(settings.safe_summary()["source_session_configured"])
        self.assertEqual(settings.static_proxy_url, "")
        self.assertEqual(settings.static_proxy_probe_timeout_seconds, 2)
        summary = settings.safe_summary()
        self.assertNotIn("bot-token", repr(summary))
        self.assertNotIn("hash-value", repr(summary))
        self.assertTrue(summary["log_file_enabled"])
        self.assertTrue(summary["auto_retry_enabled"])
        self.assertEqual(summary["auto_retry_max_attempts"], 3)
        self.assertFalse(summary["static_proxy_configured"])

    def test_static_proxy_is_deployment_only_and_never_exposed_in_summary(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_STATIC_PROXY_URL": "http://alice:super-secret@proxy.example.test:8080",
                "TGVIO_STATIC_PROXY_PROBE_TIMEOUT_SECONDS": "3",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.static_proxy_probe_timeout_seconds, 3)
        self.assertTrue(settings.safe_summary()["static_proxy_configured"])
        self.assertNotIn("alice", repr(settings.safe_summary()))
        self.assertNotIn("super-secret", repr(settings.safe_summary()))
        self.assertNotIn("proxy.example.test", repr(settings.safe_summary()))
        self.assertNotIn("alice", repr(settings))
        self.assertNotIn("super-secret", repr(settings))
        self.assertNotIn("proxy.example.test", repr(settings))

        for value in (
            "relative-proxy",
            "ftp://proxy.example.test:21",
            "http://proxy.example.test:8080/private",
            "http://proxy.example.test:bad",
            "http://proxy.example.test:0",
            "http://[malformed",
        ):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {**BASE_ENV, "TGVIO_STATIC_PROXY_URL": value},
                clear=True,
            ):
                with self.assertRaisesRegex(ConfigError, "STATIC_PROXY_URL"):
                    Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_STATIC_PROXY_PROBE_TIMEOUT_SECONDS": "11"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "STATIC_PROXY_PROBE_TIMEOUT_SECONDS"):
                Settings.from_env()

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
        self.assertEqual(settings.archive_profile_id, "primary")
        self.assertEqual(settings.archive_policy, "required")
        self.assertEqual(settings.archive_poll_seconds, 10)
        self.assertNotIn("archive_password", settings.safe_summary())
        self.assertEqual(settings.safe_summary()["archive_profile_id"], "primary")
        self.assertEqual(settings.safe_summary()["archive_policy"], "required")

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
            {**BASE_ENV, "TGVIO_ARCHIVE_PROFILE_ID": "../secret"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_PROFILE_ID"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_ARCHIVE_POLICY": "mirror_everything"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_POLICY"):
                Settings.from_env()
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


class SourceReaderSettingsTests(unittest.TestCase):
    def test_source_reader_values_are_parsed_and_summarized(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_SOURCE_SESSION": "/data/source_user",
                "TGVIO_SOURCE_CHATS": "@a, -1001234567890",
                "TGVIO_SOURCE_DOWNLOAD_WORKERS": "6",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.source_session, Path("/data/source_user"))
        self.assertEqual(settings.source_chats, ("@a", "-1001234567890"))
        self.assertEqual(settings.source_download_workers, 6)
        summary = settings.safe_summary()
        self.assertEqual(summary["source_chat_count"], 2)
        self.assertNotIn("-1001234567890", repr(summary))

    def test_source_reader_can_be_disabled_with_empty_session(self) -> None:
        with patch.dict(
            os.environ, {**BASE_ENV, "TGVIO_SOURCE_SESSION": ""}, clear=True
        ):
            settings = Settings.from_env()
        self.assertIsNone(settings.source_session)
        self.assertFalse(settings.safe_summary()["source_session_configured"])

    def test_source_download_workers_range(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_SOURCE_DOWNLOAD_WORKERS": "99"},
            clear=True,
        ), self.assertRaises(ConfigError):
            Settings.from_env()

    def test_merge_max_items_defaults_and_validates(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.source_merge_max_items, 100)
        self.assertEqual(
            settings.safe_summary()["source_merge_max_items"], 100
        )

        with patch.dict(
            os.environ, {**BASE_ENV, "TGVIO_MERGE_MAX_ITEMS": "300"}, clear=True
        ):
            self.assertEqual(Settings.from_env().source_merge_max_items, 300)

        with patch.dict(
            os.environ, {**BASE_ENV, "TGVIO_MERGE_MAX_ITEMS": "0"}, clear=True
        ), self.assertRaises(ConfigError):
            Settings.from_env()


class YtdlpCookieSettingsTests(unittest.TestCase):
    def test_cookies_path_is_optional_and_redacted(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_YTDLP_COOKIES_FILE": "/app/session/cookies.txt"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.ytdlp_cookies_file, "/app/session/cookies.txt")
        summary = settings.safe_summary()
        self.assertTrue(summary["ytdlp_cookies_configured"])
        self.assertNotIn("/app/session/cookies.txt", repr(summary))

        with patch.dict(os.environ, BASE_ENV, clear=True):
            default = Settings.from_env()
        self.assertFalse(default.safe_summary()["ytdlp_cookies_configured"])


class DashboardWebhookSettingsTests(unittest.TestCase):
    def test_defaults_keep_operations_surface_disabled(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.dashboard_enabled)
        self.assertEqual(settings.dashboard_host, "127.0.0.1")
        self.assertEqual(settings.dashboard_port, 8787)
        self.assertFalse(settings.webhook_enabled)
        self.assertEqual(settings.notification_poll_seconds, 15)
        summary = settings.safe_summary()
        self.assertFalse(summary["dashboard_enabled"])
        self.assertFalse(summary["webhook_enabled"])
        self.assertNotIn("dashboard_token", summary)

    def test_dashboard_requires_loopback_and_strong_token(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_DASHBOARD_ENABLED": "true", "TGVIO_DASHBOARD_TOKEN": "short"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "DASHBOARD_TOKEN"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_DASHBOARD_ENABLED": "true",
                "TGVIO_DASHBOARD_TOKEN": "a" * 40,
                "TGVIO_DASHBOARD_HOST": "0.0.0.0",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "DASHBOARD_HOST"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_DASHBOARD_ENABLED": "true",
                "TGVIO_DASHBOARD_TOKEN": "a" * 40,
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.dashboard_enabled)
        self.assertEqual(settings.safe_summary()["dashboard_host_class"], "loopback")
        self.assertNotIn("a" * 40, repr(settings.safe_summary()))

    def test_webhook_requires_https_and_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_WEBHOOK_ENABLED": "true",
                "TGVIO_WEBHOOK_URL": "http://example.test/hook",
                "TGVIO_WEBHOOK_TOKEN": "token",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "WEBHOOK_URL"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_WEBHOOK_ENABLED": "true",
                "TGVIO_WEBHOOK_URL": "https://example.test/hook",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "WEBHOOK_TOKEN"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_WEBHOOK_ENABLED": "true",
                "TGVIO_WEBHOOK_URL": "https://user:pw@example.test/hook",
                "TGVIO_WEBHOOK_TOKEN": "token",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "WEBHOOK_URL"):
                Settings.from_env()


class AlertPreviewSettingsTests(unittest.TestCase):
    def test_alerts_and_preview_default_on(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.alerts_enabled)
        self.assertTrue(settings.collection_preview_enabled)
        self.assertTrue(settings.preview_enabled)
        self.assertTrue(settings.collection_editing_enabled)
        self.assertIsNone(settings.alert_user_id)
        self.assertEqual(settings.alert_cooldown_seconds, 3600)
        self.assertEqual(settings.alert_poll_seconds, 60)
        summary = settings.safe_summary()
        self.assertTrue(summary["alerts_enabled"])
        self.assertTrue(summary["collection_preview_enabled"])

    def test_alert_user_must_be_integer_and_toggles_apply(self) -> None:
        with patch.dict(os.environ, {**BASE_ENV, "TGVIO_ALERT_USER_ID": "not-a-number"}, clear=True):
            with self.assertRaisesRegex(ConfigError, "ALERT_USER_ID"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "TGVIO_ALERTS_ENABLED": "false",
                "TGVIO_COLLECTION_PREVIEW_ENABLED": "false",
                "TGVIO_ALERT_USER_ID": "99",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertFalse(settings.alerts_enabled)
        self.assertFalse(settings.collection_preview_enabled)
        self.assertEqual(settings.alert_user_id, 99)


class ArchiveTimeoutSettingsTests(unittest.TestCase):
    def test_archive_timeouts_default_to_slow_backend_tolerant(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.archive_response_timeout_seconds, 3600)
        self.assertEqual(settings.archive_verify_attempts, 120)
        self.assertEqual(settings.archive_verify_interval_seconds, 20)
        summary = settings.safe_summary()
        self.assertEqual(summary["archive_response_timeout_seconds"], 3600)

    def test_archive_timeouts_validate_range(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_ARCHIVE_RESPONSE_TIMEOUT_SECONDS": "10"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_RESPONSE_TIMEOUT_SECONDS"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {**BASE_ENV, "TGVIO_ARCHIVE_VERIFY_ATTEMPTS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "ARCHIVE_VERIFY_ATTEMPTS"):
                Settings.from_env()
