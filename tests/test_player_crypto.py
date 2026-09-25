from __future__ import annotations

import base64
import unittest

from tgvio_player.infrastructure.player_crypto import PlayerStateCipher


def recovery_key(raw: bytes = b"r" * 32) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class PlayerStateCipherTests(unittest.TestCase):
    def test_round_trip_uses_a_fresh_nonce_and_context_bound_authentication(self) -> None:
        cipher = PlayerStateCipher(recovery_key())
        first = cipher.encrypt(b"private state", context=b"config")
        second = cipher.encrypt(b"private state", context=b"config")

        self.assertNotEqual(first, second)
        self.assertEqual(cipher.decrypt(first, context=b"config"), b"private state")
        with self.assertRaises(ValueError):
            cipher.decrypt(first, context=b"manifest")

    def test_tampering_and_malformed_envelopes_are_rejected(self) -> None:
        cipher = PlayerStateCipher(recovery_key())
        envelope = cipher.encrypt(b"private state", context=b"config")
        tampered = bytearray(envelope)
        tampered[-1] ^= 1

        with self.assertRaises(ValueError):
            cipher.decrypt(bytes(tampered), context=b"config")
        for malformed in (b"", b"{}", b"TGVIO\x01"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                cipher.decrypt(malformed, context=b"config")

    def test_recovery_key_must_decode_to_exactly_256_bits(self) -> None:
        for invalid in ("", recovery_key(b"short"), "not base64 !"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PlayerStateCipher(invalid)


if __name__ == "__main__":
    unittest.main()
