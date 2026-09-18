from __future__ import annotations

import unittest

from tgvio.domain.telegram_links import is_telegram_link, parse_telegram_link


class TelegramLinkTests(unittest.TestCase):
    def test_public_username_message_link(self) -> None:
        link = parse_telegram_link("https://t.me/example_channel/123")
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.chat, "@example_channel")
        self.assertEqual(link.message_id, 123)

    def test_private_channel_link_maps_to_internal_id(self) -> None:
        link = parse_telegram_link("https://t.me/c/1234567890/42?single")
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.chat, -1001234567890)
        self.assertEqual(link.message_id, 42)

    def test_bare_host_and_dog_host_are_accepted(self) -> None:
        self.assertEqual(parse_telegram_link("t.me/foo/7").chat, "@foo")  # type: ignore[union-attr]
        self.assertEqual(parse_telegram_link("telegram.dog/foo/7").message_id, 7)  # type: ignore[union-attr]

    def test_tg_scheme_is_accepted(self) -> None:
        link = parse_telegram_link("tg://resolve?domain=foo&post=9")
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.chat, "@foo")
        self.assertEqual(link.message_id, 9)

    def test_non_message_links_are_rejected(self) -> None:
        for value in (
            "https://example.com/watch?v=1",
            "https://t.me/joinchat/AAAA",
            "https://t.me/+AbCdEf",
            "https://t.me/onlyusername",
            "t.me/foo/notanumber",
            "",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_telegram_link(value))
                self.assertFalse(is_telegram_link(value))

    def test_is_telegram_link_matches_parser(self) -> None:
        self.assertTrue(is_telegram_link("https://t.me/foo/1"))
        self.assertFalse(is_telegram_link("not a link"))


if __name__ == "__main__":
    unittest.main()
