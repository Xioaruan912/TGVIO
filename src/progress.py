"""Presentation helpers and per-job progress throttling."""

from __future__ import annotations

from dataclasses import dataclass
import time


def render_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "█" * filled + "░" * (width - filled)


def position_token(n: int) -> str:
    if 1 <= n <= 9:
        return "①②③④⑤⑥⑦⑧⑨"[n - 1]
    return str(n)


@dataclass
class ProgressState:
    phase: str
    generation: int
    received: int = 0
    total: int = 0
    item: int = 1
    items: int = 1
    pct: int = 0
    speed_bps: float | None = None
    eta_seconds: float | None = None
    sample_at: float = 0.0
    sample_bytes: int = 0
    samples: int = 0
    last_ui_at: float = 0.0
    last_db_at: float = 0.0
    last_db_bytes: int = 0


class ProgressTracker:
    """Independent progress state for every (job, phase) plus a global edit bucket."""

    def __init__(
        self,
        *,
        ui_interval: float = 2.0,
        db_interval: float = 5.0,
        db_bytes: int = 32 * 1024 * 1024,
        edit_rate: float = 1.0,
        edit_burst: float = 3.0,
    ) -> None:
        self.ui_interval = float(ui_interval)
        self.db_interval = float(db_interval)
        self.db_bytes = int(db_bytes)
        self.edit_rate = float(edit_rate)
        self.edit_burst = float(edit_burst)
        self._states: dict[tuple[int, str], ProgressState] = {}
        self._generation: dict[int, int] = {}
        self._phase: dict[int, str] = {}
        self._tokens = float(edit_burst)
        self._token_at = time.monotonic()
        self._blocked_until = 0.0

    _PHASE_RANK = {
        "queued": 0,
        "downloading": 1,
        "ready": 2,
        "publishing": 3,
        "succeeded": 4,
        "failed": 4,
        "cancelled": 4,
    }

    def begin_phase(self, seq: int, phase: str) -> int:
        current = self._phase.get(int(seq))
        if current != phase:
            self._phase[int(seq)] = phase
            self._generation[int(seq)] = self._generation.get(int(seq), 0) + 1
        return self._generation.get(int(seq), 0)

    def phase_is_stale(self, seq: int, phase: str) -> bool:
        current = self._phase.get(int(seq))
        if current is None or current == phase:
            return False
        return self._PHASE_RANK.get(phase, 0) < self._PHASE_RANK.get(current, 0)

    def update(
        self,
        seq: int,
        phase: str,
        received: int,
        total: int,
        item: int,
        items: int,
        *,
        now: float | None = None,
    ) -> ProgressState:
        now = time.monotonic() if now is None else float(now)
        self.begin_phase(seq, phase)
        key = (int(seq), str(phase))
        state = self._states.get(key)
        if state is None or state.generation != self._generation[seq]:
            state = ProgressState(phase=phase, generation=self._generation[seq])
            self._states[key] = state
        received = max(0, int(received))
        total = max(0, int(total))
        if state.sample_at and now > state.sample_at and received >= state.sample_bytes:
            instant = (received - state.sample_bytes) / (now - state.sample_at)
            if instant >= 0:
                state.speed_bps = instant if state.speed_bps is None else (0.35 * instant + 0.65 * state.speed_bps)
                state.samples += 1
        state.sample_at = now
        state.sample_bytes = received
        state.received = received
        state.total = total
        state.item = max(1, int(item))
        state.items = max(1, int(items))
        if state.items <= 1:
            state.pct = int(received * 100 / total) if total else 0
        else:
            frac = received / total if total else 0
            state.pct = round(((state.item - 1) + frac) * 100 / state.items)
        if state.speed_bps and state.samples >= 1 and total > received:
            state.eta_seconds = (total - received) / state.speed_bps
        else:
            state.eta_seconds = None
        return state

    def allow_ui(self, state: ProgressState, *, now: float | None = None, force: bool = False) -> bool:
        now = time.monotonic() if now is None else float(now)
        if now < self._blocked_until:
            return False
        elapsed = max(0.0, now - self._token_at)
        self._tokens = min(self.edit_burst, self._tokens + elapsed * self.edit_rate)
        self._token_at = now
        if not force and now - state.last_ui_at < self.ui_interval:
            return False
        if not force and self._tokens < 1.0:
            return False
        if not force:
            self._tokens -= 1.0
        state.last_ui_at = now
        return True

    def defer_ui(self, seconds: float, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        self._blocked_until = max(self._blocked_until, now + max(0.0, float(seconds)))

    def allow_db(self, state: ProgressState, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        if (
            state.last_db_at == 0.0
            or now - state.last_db_at >= self.db_interval
            or abs(state.received - state.last_db_bytes) >= self.db_bytes
        ):
            state.last_db_at = now
            state.last_db_bytes = state.received
            return True
        return False

    def generation(self, seq: int) -> int:
        return self._generation.get(int(seq), 0)

    def state(self, seq: int, phase: str) -> ProgressState | None:
        return self._states.get((int(seq), str(phase)))
