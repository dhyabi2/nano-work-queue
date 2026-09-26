"""Time, injectable, so the TTL test can advance it instead of sleeping."""

import datetime


class Clock:
    def now(self) -> float:
        import time

        return time.time()

    def now_iso(self) -> str:
        return (
            datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def iso(self, epoch: float) -> str:
        return (
            datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )


class FakeClock(Clock):
    def __init__(self, start=1_800_000_000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float):
        self._t += float(seconds)
        return self._t
