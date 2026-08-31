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


if __name__ == "__main__":
    unittest.main()
