"""Command line: python -m app.cli <command>. run.sh wraps these for Docker."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from . import feeds, ingest, topics
from .clock import SystemClock
from .config import ConfigError, Settings
from .db import Database
from .mailer import MailError, allowlist_matches, build_mailer
from .service import add_subscriber, create_schedule, make_context, run_schedule_now, scan_once


def _conn(settings):
    return Database(settings.database_url).connect(retries=10, delay=2)


def cmd_seed(a, s):
    with _conn(s) as c:
        t = topics.seed_topics(c)
        n = ingest.seed_sources(c, s.sources_file)
        c.commit()
    print(f"seed ok: {t} new topics, {n} new sources")


def cmd_add_subscriber(a, s):
    with _conn(s) as c:
        sid = add_subscriber(c, SystemClock(), a.email, a.name, a.language, a.topics.split(",") if a.topics else [])
    print(f"subscriber created: {sid}")


def cmd_create_schedule(a, s):
    with _conn(s) as c:
        sid = create_schedule(c, SystemClock(), a.name, a.time, a.timezone or s.timezone,
                              a.topics.split(",") if a.topics else [], weekdays=[int(x) for x in a.weekdays.split(",")] if a.weekdays else None)
    print(f"schedule created: {sid}")


def cmd_ingest(a, s):
    with _conn(s) as c:
        print(json.dumps(ingest.ingest_all(c, s, SystemClock()), indent=2))


def cmd_run_schedule(a, s):
    with _conn(s) as c:
        status, msg = run_schedule_now(c, make_context(s), a.schedule_id)
    print(f"{status}: {msg}")
    return 0 if status in ("sent", "pending_review", "skipped_no_new_articles", "skipped_low_relevance") else 1


def cmd_scan_once(a, s):
    with _conn(s) as c:
        print(json.dumps(scan_once(c, make_context(s)), indent=2))


def cmd_runs(a, s):
    with _conn(s) as c:
        for r in c.all("SELECT r.started_at, r.status, r.run_key, r.message, s.name FROM schedule_runs r "
                       "JOIN newsletter_schedules s ON s.id = r.schedule_id ORDER BY r.started_at DESC, r.run_key LIMIT ?", (a.limit,)):
            print(f"{r['started_at']}  {r['status']:<22} {r['name']}: {r['message']}")


def cmd_check_smtp(a, s):
    if s.email_mode != "smtp":
        print("EMAIL_MODE is 'mailpit' - nothing to check against a real server. Set EMAIL_MODE=smtp and SMTP_* in .env first.")
        return 1
    mailer, _ = build_mailer(s)
    try:
        info = mailer.check_connection()
    except MailError as exc:
        print(f"SMTP CHECK FAILED: {exc}")
        return 1
    print(f"SMTP check ok (connect + TLS only, no login, nothing sent): {info}")
    return 0


def cmd_send_test(a, s):
    if s.email_mode != "smtp":
        print("send-test needs EMAIL_MODE=smtp (and SMTP_* settings). Demo mode never sends real email.")
        return 1
    addr = a.address.strip().lower()
    if not allowlist_matches(s.real_send_allowlist, addr):
        print(f"REFUSED: {addr} is not in REAL_SEND_ALLOWLIST. Add it to .env and try again.")
        return 1
    with _conn(s) as c:
        others = c.all("SELECT email FROM subscribers WHERE is_active = 1 AND email <> ?", (addr,))
        if others:
            print(f"REFUSED: {len(others)} other active subscriber(s) exist; a real test must not be able to reach them.")
            return 1
        enabled = c.scalar("SELECT COUNT(*) FROM newsletter_schedules WHERE enabled = 1")
        if enabled:
            print(f"REFUSED: {enabled} enabled schedule(s) exist; disable them before a real send test.")
            return 1
        clock = SystemClock()
        row = c.one("SELECT id FROM subscribers WHERE email = ?", (addr,))
        if not row:
            add_subscriber(c, clock, addr, "Test Recipient", s.default_language,
                           ["ai_news", "technology_science", "business", "world_news"])
        else:
            c.run("UPDATE subscribers SET is_active = 1 WHERE id = ?", (row["id"],))
            c.commit()
        sid = create_schedule(c, clock, "send-test", "00:00", s.timezone, enabled=False)   # created disabled: the worker never runs it
        ingest.ensure_fresh_ingest(c, s, clock)
        status, msg = run_schedule_now(c, make_context(s), sid)
    print(f"send-test result: {status}: {msg}")
    return 0 if status == "sent" else 1


def cmd_check_feeds(a, s):
    with _conn(s) as c:
        rows = c.all("SELECT * FROM sources WHERE mode = 'live' AND enabled = 1 ORDER BY source_key")
    now = SystemClock().now()
    window = timedelta(days=s.feed_freshness_days)
    for src in rows:
        print(f"== {src['source_key']}  {src['feed_url']}")
        try:
            entries = feeds.parse_feed(feeds.fetch_live(src["feed_url"]))
        except Exception as exc:  # noqa: BLE001
            print(f"   FAILED: {exc}")
            continue
        kept = 0
        for e in entries:
            age = now - e.published_at
            keep = age <= window
            kept += keep
            print(f"   {'KEEP' if keep else 'DROP'}  age {age.total_seconds() / 86400:5.1f} d  {e.title[:70]}")
        print(f"   -> {len(entries)} entries, {kept} inside the {s.feed_freshness_days}-day window")
    return 0


def cmd_check_llm(a, s):
    print(f"LLM_PROVIDER={s.llm_provider}: the deterministic mock provider is active. It makes no network calls and needs no key.")
    print("Groq / Ollama providers are not part of this build (planned Phase 2).")
    return 0


def cmd_diagnostics(a, s):
    print(json.dumps(s.redacted(), indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("seed").set_defaults(fn=cmd_seed)
    p = sp.add_parser("add-subscriber"); p.add_argument("email"); p.add_argument("name"); p.add_argument("--language", default="en"); p.add_argument("--topics", default=""); p.set_defaults(fn=cmd_add_subscriber)
    p = sp.add_parser("create-schedule"); p.add_argument("name"); p.add_argument("time"); p.add_argument("--timezone"); p.add_argument("--topics", default=""); p.add_argument("--weekdays", default=""); p.set_defaults(fn=cmd_create_schedule)
    sp.add_parser("ingest").set_defaults(fn=cmd_ingest)
    p = sp.add_parser("run-schedule"); p.add_argument("schedule_id"); p.set_defaults(fn=cmd_run_schedule)
    sp.add_parser("scan-once").set_defaults(fn=cmd_scan_once)
    p = sp.add_parser("runs"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_runs)
    sp.add_parser("check-smtp").set_defaults(fn=cmd_check_smtp)
    p = sp.add_parser("send-test"); p.add_argument("address"); p.set_defaults(fn=cmd_send_test)
    sp.add_parser("check-feeds").set_defaults(fn=cmd_check_feeds)
    sp.add_parser("check-llm").set_defaults(fn=cmd_check_llm)
    sp.add_parser("diagnostics").set_defaults(fn=cmd_diagnostics)
    args = ap.parse_args(argv)
    try:
        settings = Settings.from_env()
        return args.fn(args, settings) or 0
    except (ConfigError, ValueError, ConnectionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
