"""Scheduler worker: scans for due schedules every SCHEDULE_SCAN_SECONDS. Exits cleanly on SIGTERM."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import timedelta

from .clock import SystemClock, iso, parse_iso
from .config import ConfigError, Settings
from .db import Database
from .service import make_context, scan_once

log = logging.getLogger("worker")


def healthcheck(settings: Settings) -> int:
    conn = Database(settings.database_url).connect()
    try:
        beat = conn.scalar("SELECT value FROM automation_state WHERE state_key = 'worker_heartbeat'")
    finally:
        conn.close()
    if not beat:
        print("no heartbeat yet")
        return 1
    age = SystemClock().now() - parse_iso(beat)
    ok = age <= timedelta(seconds=max(90, settings.schedule_scan_seconds * 3))
    print(f"heartbeat {int(age.total_seconds())}s ago")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--healthcheck", action="store_true")
    args = ap.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.healthcheck:
        return healthcheck(settings)
    ctx = make_context(settings)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    db = Database(settings.database_url)
    log.info("worker started: scan every %ss, catch-up %s min, email mode %s, feed mode %s",
             settings.schedule_scan_seconds, settings.schedule_catchup_minutes, settings.email_mode, settings.feed_mode)
    while not stop.is_set():
        try:
            conn = db.connect(retries=5)
            try:
                s = scan_once(conn, ctx)
            finally:
                conn.close()
            log.info("scan: %d schedules, %d due, %d claimed, %d already done, %d invalid, %d errors, statuses=%s",
                     s["schedules"], s["due"], s["claimed"], s["already_done"], s["invalid"], s["errors"], s["statuses"])
        except Exception:  # noqa: BLE001
            log.exception("scan failed (will retry)")
        if args.once:
            break
        stop.wait(settings.schedule_scan_seconds)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
