from __future__ import annotations

import base64
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock

from tgvio_player.domain.ranges import ByteRange

from tgvio_player.infrastructure.webdav_aiohttp import (
    AioHttpReadOnlyWebDavClient,
    WebDavClientSettings,
)
from tgvio_player.infrastructure.cover_mirror import CoverMirrorCounters
from tgvio_player.main import PlayerSettings, build_cover_mirror


def player_env(**overrides: str) -> dict[str, str]:
    values = {
        "TGVIO_PLAYER_ENABLED": "true",
        "TGVIO_PLAYER_DATA_DIR": "/var/lib/tgvio-player",
        "TGVIO_PLAYER_ACCESS_SECRET": "s" * 32,
        "TGVIO_PLAYER_RECOVERY_KEY": base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("="),
        "TGVIO_PLAYER_WEBDAV_URL": "https://webdav.example.invalid/archive",
        "TGVIO_PLAYER_WEBDAV_USER": "reader",
        "TGVIO_PLAYER_WEBDAV_PASSWORD": "password",
        "TGVIO_PLAYER_REMOTE_ROOT": "TGVIO",
    }
    values.update(overrides)
    return values


class PlayerRuntimeSettingsTests(unittest.TestCase):
    def test_the_cover_mirror_is_on_by_default_and_bounded(self) -> None:
        settings = PlayerSettings.from_env(player_env())
        self.assertTrue(settings.cover_mirror, "the mirror is on unless it is turned off")
        self.assertEqual(settings.cover_mirror_bytes, 268435456)
        self.assertEqual(settings.cover_mirror_batch, 64)
        self.assertEqual(settings.cover_mirror_concurrency, 2)
        self.assertEqual(settings.cover_mirror_interval_seconds, 30,
                         "the catch-up cadence the spec fixes, not the idle backoff")
        self.assertEqual(settings.webdav_internal_url, "")
        internal = PlayerSettings.from_env(
            player_env(TGVIO_PLAYER_WEBDAV_INTERNAL_URL="http://host.docker.internal:5244/dav/")
        )
        self.assertEqual(internal.webdav_internal_url, "http://host.docker.internal:5244/dav")
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_WEBDAV_INTERNAL_URL="http://u:p@host/dav"))
        off = PlayerSettings.from_env(player_env(TGVIO_PLAYER_COVER_MIRROR="off"))
        self.assertFalse(off.cover_mirror)
        # This file's convention is fail-closed: a value off the list is an error, never a
        # silent fallback that would ship a mirror nobody asked for.
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_COVER_MIRROR="maybe"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_COVER_MIRROR_BYTES="0"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_COVER_MIRROR_BATCH="many"))

    def test_the_mirror_lives_under_the_data_directory_and_is_absent_when_off(self) -> None:
        settings = PlayerSettings.from_env(player_env(TGVIO_PLAYER_DATA_DIR="/tmp/tgvio-mirror-test"))
        mirror, counters = build_cover_mirror(settings)
        self.assertEqual(mirror.root, Path("/tmp/tgvio-mirror-test") / "covers")
        self.assertEqual(mirror.budget_bytes, settings.cover_mirror_bytes)
        self.assertEqual(counters, CoverMirrorCounters())
        self.assertEqual(
            build_cover_mirror(PlayerSettings.from_env(player_env(TGVIO_PLAYER_COVER_MIRROR="off"))),
            (None, None),
            "off builds nothing at all",
        )

    def test_only_player_namespace_is_read_and_defaults_are_bounded(self) -> None:
        settings = PlayerSettings.from_env({**player_env(), "BOT_TOKEN": "must-not-be-read"})
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 8790)
        self.assertEqual(settings.catalog_poll_seconds, 60)

    def test_default_client_stream_limit_matches_the_global_limit(self) -> None:
        settings = PlayerSettings.from_env(player_env())
        self.assertEqual(settings.max_streams_per_client, settings.max_streams)

    def test_disabled_or_weak_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_ENABLED="false"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_ACCESS_SECRET="short"))
        self.assertEqual(
            PlayerSettings.from_env(
                player_env(TGVIO_PLAYER_ACCESS_SECRET="214253551")
            ).access_secret,
            "214253551",
        )
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_ACCESS_SECRET="12345678"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_HOST="localhost"))
        with self.assertRaises(ValueError):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_PORT="0"))

    def test_recovery_key_is_required_and_must_be_valid_without_echoing_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "TGVIO_PLAYER_RECOVERY_KEY"):
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_RECOVERY_KEY=""))

        exposed_value = "short-secret-value"
        with self.assertRaises(ValueError) as raised:
            PlayerSettings.from_env(player_env(TGVIO_PLAYER_RECOVERY_KEY=exposed_value))
        self.assertIn("TGVIO_PLAYER_RECOVERY_KEY", str(raised.exception))
        self.assertNotIn(exposed_value, str(raised.exception))

        configured = player_env()
        self.assertTrue(PlayerSettings.from_env(configured).recovery_key)


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

    def test_plain_http_only_for_an_explicit_internal_address(self) -> None:
        client = AioHttpReadOnlyWebDavClient(
            WebDavClientSettings("http://host.docker.internal:5244/dav", "user", "pass", allow_plain_http=True)
        )
        self.assertEqual(client._url("TGVIO/a.mp4"), "http://host.docker.internal:5244/dav/TGVIO/a.mp4")
        with self.assertRaises(ValueError):
            AioHttpReadOnlyWebDavClient(
                WebDavClientSettings("ftp://host.docker.internal/dav", "user", "pass", allow_plain_http=True)
            )
        with self.assertRaises(ValueError):
            AioHttpReadOnlyWebDavClient(
                WebDavClientSettings("http://u:p@host.docker.internal/dav", "user", "pass", allow_plain_http=True)
            )

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


class PlayerWebDavBodyOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def response_body(self, chunks):
        async def content():
            for chunk in chunks:
                yield chunk
        response = Mock(status=503, headers={})
        response.content.iter_chunked.return_value = content()
        client = AioHttpReadOnlyWebDavClient(
            WebDavClientSettings("https://dav.example.invalid/archive", "user", "pass")
        )
        client._request = AsyncMock(return_value=response)
        upstream = await client.open_range("TGVIO/clip.jpg", ByteRange(0, 3))
        return response, upstream.body

    async def test_unread_body_close_releases_the_response(self):
        response, body = await self.response_body([b"error"])
        await body.aclose()
        response.release.assert_called_once()
        await body.aclose()
        response.release.assert_called_once()
        self.assertEqual([chunk async for chunk in body], [])

    async def test_started_and_exhausted_bodies_release_exactly_once(self):
        response, body = await self.response_body([b"first", b"last"])
        self.assertEqual(await anext(body), b"first")
        await body.aclose()
        response.release.assert_called_once()
        response, body = await self.response_body([b"whole"])
        self.assertEqual([chunk async for chunk in body], [b"whole"])
        response.release.assert_called_once()
        await body.aclose()
        response.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
