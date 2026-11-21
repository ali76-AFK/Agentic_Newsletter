"""End-to-end smoke test against the REAL running stack (real DB, real worker, Mailpit HTTP API).

    ./run.sh smoke        (runs this inside the tools container)

It never calls the pipeline in-process for the main scenario: it creates a due schedule and waits for the
worker container to pick it up, then checks what Mailpit actually received.
"""
from __future__ import annotations

import email
import email.policy
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from . import ingest
from .clock import SystemClock
from .config import ConfigError, Settings
from .db import Database
from .service import add_subscriber, create_schedule, make_context, run_schedule_now


class Mailpit:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, path: str, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()

    def messages(self) -> list[dict]:
        return json.loads(self._req("/api/v1/messages?limit=500"))["messages"]

    def raw(self, mid: str) -> email.message.EmailMessage:
        return email.message_from_bytes(self._req(f"/api/v1/message/{mid}/raw"), policy=email.policy.default)

    def clear(self) -> None:
        self._req("/api/v1/messages", method="DELETE")


class Report:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""), flush=True)
        return ok

    def verdict(self) -> int:
        failed = [(n, d) for n, ok, d in self.rows if not ok]
        if failed:
            print("\nRESULT: FAIL")
            for n, d in failed:
                print(f"  - {n}: {d}")
            return 1
        print("\nRESULT: PASS")
        return 0


def wait_for(fn, timeout: float, interval: float = 2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


def main() -> int:
    try:
        s = Settings.from_env()
    except ConfigError as exc:
        print(f"RESULT: FAIL\n  - configuration: {exc}")
        return 1
    rep = Report()
    if s.email_mode != "mailpit":
        print("RESULT: FAIL\n  - smoke needs EMAIL_MODE=mailpit (it must never send real email)")
        return 1
    if s.schedule_catchup_minutes < 5:
        print("RESULT: FAIL\n  - smoke needs SCHEDULE_CATCHUP_MINUTES >= 5 (it schedules a run 3 minutes in the past)")
        return 1
    rid = uuid.uuid4().hex[:8]
    mp = Mailpit(s.mailpit_api_url)
    conn = Database(s.database_url).connect(retries=10, delay=2)
    clock = SystemClock()
    print(f"smoke run {rid}: db ok, mailpit {s.mailpit_api_url}, scan every {s.schedule_scan_seconds}s")
    try:
        mp.messages()
        rep.check("Mailpit API reachable", True)
    except Exception as exc:  # noqa: BLE001
        rep.check("Mailpit API reachable", False, f"{type(exc).__name__}: {exc}")
        return rep.verdict()
    mp.clear()
    # keep the demo isolated from earlier smoke runs
    conn.run("UPDATE subscribers SET is_active = 0 WHERE email LIKE ?", ("%@smoke.test",))
    conn.run("UPDATE newsletter_schedules SET enabled = 0 WHERE name LIKE ?", ("smoke-%",))
    conn.commit()

    alice, bob = f"alice-{rid}@smoke.test", f"bob-{rid}@smoke.test"
    add_subscriber(conn, clock, alice, "Alice", "en", ["ai_news"])
    add_subscriber(conn, clock, bob, "Bob", "de", ["robotics"])
    fixture_settings = s.with_(feed_mode="fixture")
    st = ingest.ingest_all(conn, fixture_settings, clock, fixture_tag=f"smoke-{rid}")
    rep.check("fixture articles loaded", st["new"] > 0, str(st))

    tz = ZoneInfo(s.timezone)
    due = (clock.now() - timedelta(minutes=3)).astimezone(tz)
    sched_id = create_schedule(conn, clock, f"smoke-{rid}", due.strftime("%H:%M"), s.timezone, ["ai_news", "robotics"], weekdays=[due.weekday()])
    print(f"  schedule {sched_id} due at {due.strftime('%H:%M')} {s.timezone} (3-4 min ago); waiting for the worker...", flush=True)

    def finished():
        row = conn.one("SELECT status, message FROM schedule_runs WHERE schedule_id = ? AND status <> 'running'", (sched_id,))
        conn.commit()   # end the read transaction so we see the worker's commits
        return row
    row = wait_for(finished, timeout=s.schedule_scan_seconds * 3 + 90)
    rep.check("worker picked up the due schedule", row is not None, "no finished schedule_runs row appeared - is the worker running? try ./run.sh logs worker")
    if row is None:
        return rep.verdict()
    rep.check("run status is 'sent'", row["status"] == "sent", f"{row['status']}: {row['message']}")

    msgs = mp.messages()
    for addr, expect, forbid in ((alice, "neural network", "Roboter"), (bob, "Roboter", "neural network")):
        mine = [m for m in msgs if addr in [t["Address"].lower() for t in m["To"]]]
        rep.check(f"{addr.split('-')[0]} received exactly one email", len(mine) == 1, f"got {len(mine)}")
        if len(mine) != 1:
            continue
        m = mine[0]
        rep.check(f"{addr.split('-')[0]}: To contains only them", [t["Address"].lower() for t in m["To"]] == [addr], str(m["To"]))
        raw = mp.raw(m["ID"])
        body = raw.get_body(("plain",)).get_content()
        rep.check(f"{addr.split('-')[0]}: content matches topic", expect.lower() in body.lower() and forbid.lower() not in body.lower(),
                  f"expected '{expect}', must not contain '{forbid}'")
        rep.check(f"{addr.split('-')[0]}: unsubscribe headers present",
                  bool(raw["List-Unsubscribe"]) and "One-Click" in str(raw["List-Unsubscribe-Post"]), "missing List-Unsubscribe(-Post)")
    before = len([m for m in msgs if any(t["Address"].lower() in (alice, bob) for t in m["To"])])

    wait = s.schedule_scan_seconds * 2 + 3
    print(f"  waiting {wait}s for two more worker scans (duplicate check)...", flush=True)
    time.sleep(wait)
    after = len([m for m in mp.messages() if any(t["Address"].lower() in (alice, bob) for t in m["To"])])
    conn.commit()
    n_runs = conn.scalar("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = ?", (sched_id,))
    rep.check("no duplicate emails after further scans", after == before, f"{before} -> {after}")
    rep.check("exactly one schedule_runs row for that moment", n_runs == 1, f"{n_runs} rows")

    # controlled failure: unreachable mail server, run in-process on a disabled schedule so the worker ignores it
    carol = f"carol-{rid}@smoke.test"
    add_subscriber(conn, clock, carol, "Carol", "en", ["devops"])
    fail_sched = create_schedule(conn, clock, f"smoke-fail-{rid}", "00:00", s.timezone, ["devops"], enabled=False)
    status, msg = run_schedule_now(conn, make_context(s.with_(mailpit_smtp_port=1, feed_mode="fixture")), fail_sched)
    conn.commit()
    persisted = conn.one("SELECT status, message FROM schedule_runs WHERE schedule_id = ?", (fail_sched,))
    rep.check("unreachable mail server -> persisted 'failed' run", bool(persisted) and persisted["status"] == "failed",
              f"{persisted}")
    rep.check("failure reason names the stage", bool(persisted) and "[connecting]" in (persisted["message"] or ""), f"{persisted}")
    return rep.verdict()


if __name__ == "__main__":
    sys.exit(main())
