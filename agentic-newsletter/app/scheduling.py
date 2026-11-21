"""Timezone-aware schedule evaluation with a catch-up window.

A schedule fires for a *scheduled moment* (UTC instant). A moment is due when
    moment <= now <= moment + catchup
The idempotency key is derived from the moment, never from "now".
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import UTC

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleError(Exception):
    pass


def parse_schedule(sched: dict) -> tuple[ZoneInfo, int, int, set[int]]:
    tz = sched.get("timezone") or ""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        raise ScheduleError(f"unknown timezone {tz!r} (use an IANA name like Europe/Berlin)") from None
    m = _TIME.match(str(sched.get("delivery_time") or ""))
    if not m:
        raise ScheduleError(f"delivery_time {sched.get('delivery_time')!r} is not HH:MM (24h)")
    try:
        days = json.loads(sched.get("weekdays_json") or "null")
    except ValueError:
        raise ScheduleError("weekdays_json is not valid JSON") from None
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ScheduleError("weekdays must be a non-empty list of integers 0..6 (Monday=0)")
    return zone, int(m.group(1)), int(m.group(2)), set(days)


def scheduled_moment(zone: ZoneInfo, day, hh: int, mm: int) -> datetime:
    """UTC instant for a local wall time. Nonexistent (DST gap) times move to the first valid instant after the gap.
    Ambiguous (DST overlap) times use the first occurrence, so a day never fires twice."""
    naive = datetime(day.year, day.month, day.day, hh, mm)
    utc0 = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if utc0.astimezone(zone).replace(tzinfo=None) == naive:
        return utc0
    t = min(utc0, naive.replace(tzinfo=zone, fold=1).astimezone(UTC))
    for _ in range(240):
        if t.astimezone(zone).replace(tzinfo=None) >= naive:
            return t
        t += timedelta(minutes=1)
    return utc0


def due_moments(sched: dict, now: datetime, catchup_minutes: int) -> list[datetime]:
    zone, hh, mm, days = parse_schedule(sched)
    start = now - timedelta(minutes=catchup_minutes)
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = now.astimezone(zone).date() + timedelta(days=1)
    out = []
    d = first
    while d <= last:
        if d.weekday() in days:
            m = scheduled_moment(zone, d, hh, mm)
            if start <= m <= now:
                out.append(m)
        d += timedelta(days=1)
    return sorted(out)
