"""An explicit clock so tests never sleep."""

from datetime import datetime, timedelta, timezone


class FakeClock:
    def __init__(self, now=None):
        self.set(now or datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc))

    def now(self):
        return self._now

    def set(self, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock requires a timezone-aware datetime")
        self._now = value

    def advance(self, seconds=0, **kwargs):
        delta = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds, **kwargs)
        if delta < timedelta(0):
            raise ValueError("clock cannot advance backwards")
        self._now += delta
        return self._now
