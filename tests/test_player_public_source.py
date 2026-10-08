from __future__ import annotations

import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
# Exact byte lengths and SHA-256s of storage locations from the private VPS
# migration chain. Never put those locations' plaintext into this repository.
PRIVATE_FRAGMENTS = (
    (23, bytes.fromhex("1103138cd6f255aed2b5ebaedf70ab29b4fca781d52b0e87d600726968bc2dcc")),
    (20, bytes.fromhex("775f9acd8990bc9ecc2c820c4e371453c9713971d50bcae3c122fd5bee615664")),
    (9, bytes.fromhex("3c8fd7bde2ed8fd4da876902e4e468c49b09bbc0f126bb192b75ff62c067a7e9")),
    (27, bytes.fromhex("e73371ffa49234ed0a3b366df268e3fa1f55122616cc36b102ea07e477d82933")),
)


def player_source_files() -> list[Path]:
    # The front end moved to TGVIO-Player, whose hygiene gate scans the same digests.
    paths = [path for path in (ROOT / "src/tgvio_player").rglob("*") if path.is_file()]
    paths.extend(ROOT.glob("tests/test_player*.py"))
    paths.extend(ROOT.glob("deploy/player*.example"))
    return sorted(set(paths))


class PlayerPublicSourceTests(unittest.TestCase):
    def test_public_player_sources_do_not_embed_private_storage_locations(self) -> None:
        matches: list[str] = []
        for path in player_source_files():
            data = path.read_bytes()
            for length, digest in PRIVATE_FRAGMENTS:
                if any(
                    hashlib.sha256(data[start:start + length]).digest() == digest
                    for start in range(len(data) - length + 1)
                ):
                    matches.append(str(path.relative_to(ROOT)))
                    break
        self.assertEqual(matches, [], "private Player storage location in public source files")


if __name__ == "__main__":
    unittest.main()
