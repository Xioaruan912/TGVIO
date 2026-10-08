from __future__ import annotations

import time

from tgvio_player.domain.read_mode import DEFAULT_READ_MODE, parse_read_mode


class PlayerReadModeRepositoryMixin:
    async def get_read_mode(self) -> str:
        row = self._require().execute(
            "SELECT mode FROM player_read_mode WHERE singleton=1"
        ).fetchone()
        return DEFAULT_READ_MODE if row is None else parse_read_mode(row["mode"])

    async def set_read_mode(self, mode: str) -> None:
        mode = parse_read_mode(mode)
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_read_mode(singleton, mode, updated_at) VALUES(1,?,?)
                ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at
                """,
                (mode, int(time.time())),
            )
