"""Shared helpers for the web UI. The UI never touches the database: everything goes through the API."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st

UTC = timezone.utc

STATUS_ICON = {
    "sent": "✅", "approved": "✅", "auto_send": "✅",
    "partial_failure": "⚠️", "pending_review": "🟡", "review": "🟡",
    "skipped": "⏭️", "skipped_low_relevance": "⏭️", "skipped_no_new_articles": "⏭️", "skip": "⏭️",
    "failed": "❌", "rejected": "🚫", "running": "🔄",
}
STATUS_COLOR = {
    "sent": "#2e9e5b", "partial_failure": "#e0a21b", "pending_review": "#f2d13d", "failed": "#d64545",
    "running": "#4a7fd4", "skipped_low_relevance": "#9aa5b1", "skipped_no_new_articles": "#c3cad2", "rejected": "#8e5bb5",
    "skipped": "#9aa5b1",
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # Monday = 0, same as the backend


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _detail(resp: requests.Response) -> str:
    try:
        d = resp.json().get("detail")
    except (ValueError, AttributeError):
        return resp.text[:300] or f"HTTP {resp.status_code}"
    if isinstance(d, list):   # FastAPI validation errors
        return "; ".join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}" for e in d)
    return str(d) if d else f"HTTP {resp.status_code}"


class Api:
    """Thin JSON client. The base URL is read on every call so tests can point it elsewhere."""

    @property
    def base(self) -> str:
        return os.environ.get("UI_API_URL", "http://api:8000").rstrip("/")

    def request(self, method: str, path: str, body=None, params=None, timeout: float = 15):
        try:
            r = requests.request(method, self.base + path, json=body, params=params, timeout=timeout)
        except requests.Timeout:
            raise ApiError(f"the API did not answer within {int(timeout)} s ({path}); the action may still be running - check History") from None
        except requests.RequestException as exc:
            raise ApiError(f"cannot reach the API at {self.base} ({type(exc).__name__})") from None
        if r.status_code >= 400:
            raise ApiError(_detail(r), r.status_code)
        return r.json() if r.content else None

    def get(self, path, params=None, timeout=15): return self.request("GET", path, params=params, timeout=timeout)
    def post(self, path, body=None, timeout=15): return self.request("POST", path, body=body, timeout=timeout)
    def put(self, path, body=None, timeout=15): return self.request("PUT", path, body=body, timeout=timeout)
    def patch(self, path, body=None, timeout=15): return self.request("PATCH", path, body=body, timeout=timeout)
    def delete(self, path, timeout=15): return self.request("DELETE", path, timeout=timeout)


api = Api()


def load(path: str, **params):
    """GET for page rendering. If the API is unreachable, explain and stop the page."""
    try:
        return api.get(path, params=params or None)
    except ApiError as exc:
        st.error(f"Could not load data from the API ({path}): {exc}")
        st.caption("Is the stack running? In your terminal run: ./run.sh status")
        st.stop()


def call(fn, *args, **kwargs):
    """Run a write action. Returns (ok, result) and shows the API's error message on failure."""
    try:
        return True, fn(*args, **kwargs)
    except ApiError as exc:
        st.error(str(exc))
        return False, None


def flash(kind: str, message: str) -> None:
    """Show a message on the next run (after st.rerun())."""
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    f = st.session_state.pop("_flash", None)
    if f:
        getattr(st, f[0], st.info)(f[1])


# ------------------------------------------------------------------ time helpers
def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone: fall back instead of crashing the page
        return ZoneInfo("UTC")


def fmt_ts(value: str | None, tz: ZoneInfo) -> str:
    dt = parse_ts(value)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else ""


def ago(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "never"
    s = int((now - dt).total_seconds())
    if s < 0:
        return "in the future"
    for limit, unit, div in ((90, "s", 1), (5400, "min", 60), (172800, "h", 3600)):
        if s < limit:
            return f"{max(s // div, 0)} {unit} ago"
    return f"{s // 86400} d ago"


def until(dt: datetime, now: datetime) -> str:
    s = int((dt - now).total_seconds())
    if s < 3600:
        return f"in {max(s // 60, 0)} min"
    if s < 172800:
        return f"in {s // 3600} h {(s % 3600) // 60} min"
    return f"in {s // 86400} d"


def jlist(raw) -> list:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def weekdays_text(days: list[int]) -> str:
    days = sorted(set(days))
    if days == list(range(7)):
        return "every day"
    if days == list(range(5)):
        return "Mon-Fri"
    return ", ".join(WEEKDAYS[d] for d in days if 0 <= d <= 6)


def next_run(sched: dict, now: datetime) -> datetime | None:
    """Next planned delivery (UTC). Approximate around DST changes; the worker is authoritative."""
    try:
        z = ZoneInfo(sched["timezone"])
        hh, mm = (int(x) for x in sched["delivery_time"].split(":"))
        days = set(jlist(sched.get("weekdays_json")))
    except Exception:  # noqa: BLE001
        return None
    today = now.astimezone(z).date()
    for add in range(9):
        d = today + timedelta(days=add)
        if d.weekday() not in days:
            continue
        cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=z).astimezone(UTC)
        if cand > now:
            return cand
    return None


def status_label(status: str | None) -> str:
    s = status or ""
    return f"{STATUS_ICON.get(s, '•')} {s}"


def worker_state(diag: dict, now: datetime) -> tuple[bool, str]:
    beat = parse_ts(diag.get("worker_heartbeat"))
    scan = int(diag.get("config", {}).get("schedule_scan_seconds", 30) or 30)
    if not beat:
        return False, "no heartbeat yet"
    age = (now - beat).total_seconds()
    return age <= max(90, scan * 3), f"heartbeat {ago(beat, now)}"
