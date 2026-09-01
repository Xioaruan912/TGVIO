import unittest

from src.config import Settings, SettingsError


def base_env() -> dict[str, str]:
    return {
        "API_ID": "12345",
        "API_HASH": "hash-value",
        "BOT_TOKEN": "token-value",
        "DEST_CHANNEL": "@example",
        "ALLOWED_USERS": "42,43",
    }


class SettingsTests(unittest.TestCase):
    def test_required_values_fail_fast_without_echoing_secret(self) -> None:
        env = base_env()
        env.pop("BOT_TOKEN")
        with self.assertRaises(SettingsError) as caught:
            Settings.from_env(env, strict=True)
        text = str(caught.exception)
        self.assertIn("BOT_TOKEN", text)
        self.assertNotIn("token-value", text)

    def test_invalid_worker_and_part_size_ranges_fail(self) -> None:
        env = base_env()
        env["UPLOAD_WORKERS"] = "999"
        with self.assertRaises(SettingsError):
            Settings.from_env(env, strict=True)
        env = base_env()
        env["PART_SIZE_KB"] = "300"
        with self.assertRaises(SettingsError):
            Settings.from_env(env, strict=True)

    def test_safe_summary_contains_no_identity_or_secret_values(self) -> None:
        settings = Settings.from_env(base_env(), strict=True)
        rendered = repr(settings.safe_summary())
        self.assertNotIn("hash-value", rendered)
        self.assertNotIn("token-value", rendered)
        self.assertNotIn("@example", rendered)
        self.assertNotIn("42", rendered)
        self.assertEqual(settings.safe_summary()["destination_kind"], "username")

    def test_legacy_compat_defaults_can_remain_non_strict(self) -> None:
        settings = Settings.from_env({}, strict=False)
        self.assertEqual(settings.api_id, 0)
        self.assertEqual(settings.allowed_users, frozenset())

    def test_url_private_network_policy_is_validated_and_safe_to_summarize(self) -> None:
        env = base_env()
        env["URL_PRIVATE_NETWORK_POLICY"] = "block"
        settings = Settings.from_env(env, strict=True)
        self.assertEqual(settings.url_private_network_policy, "block")
        self.assertEqual(settings.safe_summary()["url_private_network_policy"], "block")

        env["URL_PRIVATE_NETWORK_POLICY"] = "invalid"
        with self.assertRaises(SettingsError):
            Settings.from_env(env, strict=True)

    def test_history_and_event_retention_are_bounded(self) -> None:
        env = base_env()
        env.update(HISTORY_RETENTION_DAYS="45", EVENT_RETENTION_DAYS="90")
        settings = Settings.from_env(env, strict=True)
        self.assertEqual(settings.history_retention_days, 45)
        self.assertEqual(settings.event_retention_days, 90)
        self.assertEqual(settings.safe_summary()["history_retention_days"], 45)

        for key, value in (("HISTORY_RETENTION_DAYS", "0"), ("EVENT_RETENTION_DAYS", "91")):
            invalid = base_env()
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(SettingsError):
                Settings.from_env(invalid, strict=True)

    def test_dashboard_requires_private_bind_and_strong_token(self) -> None:
        env = base_env()
        env.update(DASHBOARD_ENABLED="true", DASHBOARD_TOKEN="x" * 32)
        settings = Settings.from_env(env, strict=True)
        self.assertTrue(settings.dashboard_enabled)
        self.assertEqual(settings.dashboard_socket, "session/dashboard.sock")
        rendered = repr(settings.safe_summary())
        self.assertNotIn("x" * 32, rendered)

        invalid = dict(env, DASHBOARD_HOST="0.0.0.0")
        with self.assertRaises(SettingsError):
            Settings.from_env(invalid, strict=True)

    def test_dashboard_public_bind_requires_explicit_flag_and_tcp(self) -> None:
        env = base_env()
        env.update(DASHBOARD_ENABLED="true", DASHBOARD_PUBLIC_BIND="true", DASHBOARD_HOST="0.0.0.0", DASHBOARD_SOCKET="", DASHBOARD_TOKEN="x" * 32)
        self.assertEqual(Settings.from_env(env, strict=True).safe_summary()["dashboard_transport"], "tcp-public")
        with self.assertRaises(SettingsError):
            Settings.from_env(dict(env, DASHBOARD_SOCKET="session/dashboard.sock"), strict=True)
        invalid = dict(env, DASHBOARD_TOKEN="too-short")
        with self.assertRaises(SettingsError):
            Settings.from_env(invalid, strict=True)

    def test_dashboard_public_url_is_root_only_and_hidden_from_summary(self) -> None:
        env = base_env()
        env.update(
            DASHBOARD_ENABLED="true",
            DASHBOARD_TOKEN="x" * 32,
            DASHBOARD_PUBLIC_URL="http://dashboard.example:8787",
        )
        settings = Settings.from_env(env, strict=True)
        self.assertEqual(settings.dashboard_public_url, "http://dashboard.example:8787")
        rendered = repr(settings.safe_summary())
        self.assertTrue(settings.safe_summary()["dashboard_public_url_configured"])
        self.assertNotIn("dashboard.example", rendered)

        for url in (
            "ftp://dashboard.example",
            "http://user:pass@dashboard.example",
            "http://dashboard.example/private",
            "http://dashboard.example/?token=secret",
            "http://dashboard.example/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(SettingsError):
                Settings.from_env(dict(env, DASHBOARD_PUBLIC_URL=url), strict=True)

    def test_webhook_requires_https_and_hides_endpoint_and_token(self) -> None:
        env = base_env()
        env.update(
            WEBHOOK_ENABLED="true",
            WEBHOOK_URL="https://hooks.example.invalid/private",
            WEBHOOK_TOKEN="s" * 32,
        )
        settings = Settings.from_env(env, strict=True)
        rendered = repr(settings.safe_summary())
        self.assertTrue(settings.webhook_enabled)
        self.assertNotIn("hooks.example.invalid", rendered)
        self.assertNotIn("s" * 32, rendered)

        for url in (
            "http://hooks.example.invalid/private",
            "https://user:pass@hooks.example.invalid/private",
            "https://hooks.example.invalid/private#secret",
        ):
            invalid = dict(env, WEBHOOK_URL=url)
            with self.subTest(url=url), self.assertRaises(SettingsError):
                Settings.from_env(invalid, strict=True)

    def test_large_file_split_is_opt_in_and_bounded(self) -> None:
        self.assertEqual(Settings.from_env(base_env(), strict=True).large_file_policy, "reject")
        env = base_env()
        env.update(LARGE_FILE_POLICY="split", SPLIT_PART_BYTES=str(64 * 1024 * 1024))
        self.assertEqual(Settings.from_env(env, strict=True).large_file_policy, "split")
        for key, value in (("LARGE_FILE_POLICY", "anything"), ("SPLIT_PART_BYTES", "1")):
            invalid = dict(env, **{key: value})
            with self.subTest(key=key), self.assertRaises(SettingsError):
                Settings.from_env(invalid, strict=True)


if __name__ == "__main__":
    unittest.main()
