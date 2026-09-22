from __future__ import annotations

import unittest

from tgvio_player.infrastructure.webdav_aiohttp import (
    AioHttpReadOnlyWebDavClient,
    WebDavClientSettings,
)
from tgvio_player.main import PlayerSettings


def player_env(**overrides: str) -> dict[str, str]:
    values = {
        "TGVIO_PLAYER_ENABLED": "true",
        "TGVIO_PLAYER_DATA_DIR": "/var/lib/tgvio-player",
        "TGVIO_PLAYER_ACCESS_SECRET": "s" * 32,
        "TGVIO_PLAYER_WEBDAV_URL": "https://webdav.example.invalid/archive",
        "TGVIO_PLAYER_WEBDAV_USER": "reader",
        "TGVIO_PLAYER_WEBDAV_PASSWORD": "password",
        "TGVIO_PLAYER_REMOTE_ROOT": "TGVIO",
    }
    values.update(overrides)
    return values


class PlayerRuntimeSettingsTests(unittest.TestCase):
    def test_only_player_namespace_is_read_and_defaults_are_bounded(self) -> None:
        settings = PlayerSettings.from_env({**player_env(), "BOT_TOKEN": "must-not-be-read"})
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 8790)
        self.assertEqual(settings.catalog_poll_seconds, 60)

    def test_disabled_or_weak_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_ENABLED="false"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_ACCESS_SECRET="short"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_HOST="localhost"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_PORT="0"))


class PlayerWebDavTransportTests(unittest.TestCase):
    def test_transport_rejects_non_https_and_unsafe_paths(self) -> None:
        with self.assertRaises(ValueError):
            AioHttpReadOnlyWebDavClient(WebDavClientSettings("http://dav.invalid", "user", "pass"))
        client = AioHttpReadOnlyWebDavClient(
            WebDavClientSettings("https://dav.example.invalid/archive", "user", "pass")
        )
        self.assertEqual(client._url("TGVIO/2026-09-22/1/clip name.mp4"), "https://dav.example.invalid/archive/TGVIO/2026-09-22/1/clip%20name.mp4")
        with self.assertRaises(ValueError):
            client._url("../escape.mp4")

    def test_collection_href_decodes_a_single_safe_child_name(self) -> None:
        self.assertEqual(
            AioHttpReadOnlyWebDavClient._entry_name(
                "/dav/TGVIO/2026-09-22/one%20two", "TGVIO/2026-09-22"
            ),
            "one two",
        )
        self.assertIsNone(
            AioHttpReadOnlyWebDavClient._entry_name(
                "/dav/TGVIO/2026-09-22/nested/file", "TGVIO/2026-09-22"
            )
        )


if __name__ == "__main__":
    unittest.main()
