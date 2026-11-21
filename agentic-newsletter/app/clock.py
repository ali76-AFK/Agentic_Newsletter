"""Injectable clock + UTC timestamp helpers (all stored timestamps are UTC ISO strings)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_FMT = "%Y-%m-%dT%H:%M:%SZ"


class Clock:
    def now(self) -> datetime:  # aware, UTC
        return datetime.now(UTC).replace(microsecond=0)


class SystemClock(Clock):
    pass


class FixedClock(Clock):
    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _FMT).replace(tzinfo=UTC)
