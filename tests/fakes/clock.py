"""Controllable clock for timeout and watchdog characterization tests."""


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
