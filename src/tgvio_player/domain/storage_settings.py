from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlayerStorageSettings:
    endpoint_url: str
    player_root: str
    favorites_dir: str
    username_ciphertext: bytes | None = None
    password_ciphertext: bytes | None = None
    revision: int = 0
