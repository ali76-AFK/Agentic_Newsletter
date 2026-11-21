"""Business logic shared by CLI, worker and API."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import ingest
from .clock import Clock, SystemClock, iso
from .config import Settings
from .db import Conn, new_id
from .email_build import build_message, unsubscribe_url
from .llm import LLMProvider, get_provider
from .mailer import MailError, Mailer, build_mailer
from .pipeline import DraftState, PipelineError, flush_audit, run_graph
from .scheduling import ScheduleError, due_moments

log = logging.getLogger("service")


@dataclass
class Context:
    settings: Settings
    clock: Clock
    llm: LLMProvider
    mailer: Mailer
    from_header: str
    sleep: Callable[[float], None] = time.sleep
    _last_send: float = field(default=0.0, repr=False)


def make_context(settings: Settings, clock: Clock | None = None, llm: LLMProvider | None = None,
                 mailer: Mailer | None = None, sleep: Callable[[float], None] | None = None) -> Context:
    built, from_header = build_mailer(settings)
    return Context(settings, clock or SystemClock(), llm or get_provider(settings), mailer or built, from_header,
                   sleep or time.sleep)


# ------------------------------------------------------------------ subscribers
def add_subscriber(conn: Conn, clock: Clock, email: str, name: str, language: str = "en", topics: list[str] | None = None,
                   max_articles: int = 5, lookback_hours: int = 168, summary_style: str = "concise") -> str:
    email = email.strip().lower()
    if "@" not in email or " " in email:
        raise ValueError(f"invalid email address {email!r}")
    if language not in ("en", "de"):
        raise ValueError("language must be 'en' or 'de'")
    known = {r["topic_key"] for r in conn.all("SELECT topic_key FROM topics")}
    bad = [t for t in (topics or []) if t not in known]
    if bad:
        raise ValueError(f"unknown topics: {', '.join(bad)} (known: {', '.join(sorted(known))})")
    sid = new_id()
    conn.run("INSERT INTO subscribers (id, email, name, language, is_active, created_at) VALUES (?,?,?,?,1,?)",
             (sid, email, name, language, iso(clock.now())))
    for t in topics or []:
        conn.run("INSERT INTO subscriber_topics (subscriber_id, topic_key) VALUES (?,?)", (sid, t))
    conn.run("INSERT INTO subscription_preferences (subscriber_id, max_articles, lookback_hours, summary_style) VALUES (?,?,?,?)",
             (sid, max_articles, lookback_hours, summary_style))
    conn.commit()
    return sid


def unsubscribe(conn: Conn, clock: Clock, subscriber_id: str) -> bool:
    n = conn.run("UPDATE subscribers SET is_active = 0, unsubscribed_at = ? WHERE id = ? AND is_active = 1",
                 (iso(clock.now()), subscriber_id))
    conn.commit()
    return n > 0


def _prefs(conn: Conn, subscriber_id: str) -> dict:
    row = conn.one("SELECT * FROM subscription_preferences WHERE subscriber_id = ?", (subscriber_id,))
    return row or {"max_articles": 5, "lookback_hours": 168, "summary_style": "concise", "source_keys_json": "[]",
                   "excluded_keywords_json": "[]"}


# ------------------------------------------------------------------ schedules
def create_schedule(conn: Conn, clock: Clock, name: str, delivery_time: str, timezone: str, topics: list[str] | None = None,
                    source_keys: list[str] | None = None, weekdays: list[int] | None = None, enabled: bool = True) -> str:
    weekdays = list(range(7)) if weekdays is None else weekdays
    row = {"timezone": timezone, "delivery_time": delivery_time, "weekdays_json": json.dumps(weekdays)}
    from .scheduling import parse_schedule
    parse_schedule(row)   # raises ScheduleError for bad input - refuse to store broken schedules
    sid = new_id()
    conn.run("INSERT INTO newsletter_schedules (id, name, topics_json, source_keys_json, weekdays_json, delivery_time, timezone, "
             "enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
             (sid, name, json.dumps(topics or []), json.dumps(source_keys or []), json.dumps(weekdays), delivery_time,
              timezone, 1 if enabled else 0, iso(clock.now())))
    conn.commit()
    return sid


# ------------------------------------------------------------------ sending
def send_draft(conn: Conn, ctx: Context, draft_id: str) -> str:
    """Send one draft to its single recipient. Commits. Returns the new draft status."""
    d = conn.one("SELECT * FROM draft_emails WHERE id = ?", (draft_id,))
    if d is None or d["status"] == "sent":
        return d["status"] if d else "missing"
    sub = conn.one("SELECT * FROM subscribers WHERE id = ?", (d["subscriber_id"],))
    if not sub or not sub["is_active"]:
        conn.run("UPDATE draft_emails SET status='skipped', decision_reason='subscriber unsubscribed' WHERE id=?", (draft_id,))
        conn.commit()
        return "skipped"
    try:
        msg = build_message(ctx.from_header, d["recipient_email"], d["subject"], d["text_body"], d["html_body"],
                            unsubscribe_url(ctx.settings, sub["id"]), ctx.clock, draft_id)
        per_min = ctx.settings.email_max_per_minute
        if per_min > 0:
            wait = 60.0 / per_min - (time.monotonic() - ctx._last_send)
            if wait > 0:
                ctx.sleep(wait)
        ctx.mailer.send(msg, d["recipient_email"])
        ctx._last_send = time.monotonic()
    except MailError as exc:
        conn.run("UPDATE draft_emails SET status='failed', error=?, error_stage=? WHERE id=?", (str(exc)[:900], exc.stage, draft_id))
        conn.commit()
        log.warning("send failed for %s: %s", d["recipient_email"], exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - never lose the failure
        conn.run("UPDATE draft_emails SET status='failed', error=?, error_stage='send' WHERE id=?",
                 (f"{type(exc).__name__}: {exc}"[:900], draft_id))
        conn.commit()
        return "failed"
    now = iso(ctx.clock.now())
    conn.run("UPDATE draft_emails SET status='sent', sent_at=?, message_id=?, error=NULL, error_stage=NULL WHERE id=?",
             (now, msg["Message-ID"], draft_id))
    for row in conn.all("SELECT document_id FROM newsletter_source_documents WHERE draft_id = ?", (draft_id,)):
        conn.run("INSERT INTO article_deliveries (id, subscriber_id, document_id, newsletter_id, draft_id, delivered_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (new_id(), d["subscriber_id"], row["document_id"],
                                                                 d["newsletter_id"], draft_id, now))
    conn.commit()
    return "sent"


def finalize_newsletter(conn: Conn, ctx: Context, newsletter_id: str) -> tuple[str, str]:
    """Derive the honest run status from the drafts, persist it on newsletter + schedule_run."""
    drafts = conn.all("SELECT status, error, decision_reason, recipient_email FROM draft_emails WHERE newsletter_id = ?", (newsletter_id,))
    c = {k: sum(1 for d in drafts if d["status"] == k) for k in ("sent", "failed", "pending_review", "draft", "skipped", "rejected")}
    if not drafts:
        status, msg = "skipped_no_new_articles", "no active subscriber matches this schedule's topics"
    elif c["sent"] and c["failed"]:
        status = "partial_failure"
    elif c["failed"]:
        status = "failed"
    elif c["pending_review"] or c["draft"]:
        status = "pending_review"
    elif c["sent"]:
        status = "sent"
    elif any(d["decision_reason"] == "low_relevance" for d in drafts):
        status = "skipped_low_relevance"
    else:
        status = "skipped_no_new_articles"
    if drafts:
        msg = (f"{c['sent']} sent, {c['failed']} failed, {c['pending_review'] + c['draft']} pending review, "
               f"{c['skipped'] + c['rejected']} skipped of {len(drafts)} recipients")
        first_fail = next((d for d in drafts if d["status"] == "failed"), None)
        if first_fail:
            msg += f"; first failure ({first_fail['recipient_email']}): {first_fail['error']}"
    now = iso(ctx.clock.now())
    conn.run("UPDATE newsletters SET status=? WHERE id=?", (status, newsletter_id))
    conn.run("UPDATE schedule_runs SET status=?, message=?, completed_at=? WHERE newsletter_id=?", (status, msg[:1500], now, newsletter_id))
    conn.commit()
    return status, msg


def retry_failed(conn: Conn, ctx: Context, newsletter_id: str) -> tuple[str, str]:
    """Re-send only drafts that failed at send time. Already-sent recipients are never contacted again."""
    for d in conn.all("SELECT id FROM draft_emails WHERE newsletter_id = ? AND status = 'failed' AND html_body IS NOT NULL "
                      "ORDER BY created_at, id", (newsletter_id,)):
        send_draft(conn, ctx, d["id"])
    return finalize_newsletter(conn, ctx, newsletter_id)


def approve_draft(conn: Conn, ctx: Context, draft_id: str, note: str = "") -> str:
    d = conn.one("SELECT * FROM draft_emails WHERE id = ?", (draft_id,))
    if not d or d["status"] != "pending_review":
        raise ValueError("draft is not pending review")
    conn.run("UPDATE draft_emails SET reviewed_at=?, review_note=? WHERE id=?", (iso(ctx.clock.now()), note or "approved", draft_id))
    conn.commit()
    status = send_draft(conn, ctx, draft_id)
    finalize_newsletter(conn, ctx, d["newsletter_id"])
    return status


def reject_draft(conn: Conn, ctx: Context, draft_id: str, reason: str) -> None:
    d = conn.one("SELECT * FROM draft_emails WHERE id = ?", (draft_id,))
    if not d or d["status"] != "pending_review":
        raise ValueError("draft is not pending review")
    if not reason.strip():
        raise ValueError("a rejection reason is required")
    conn.run("UPDATE draft_emails SET status='rejected', reviewed_at=?, review_note=? WHERE id=?",
             (iso(ctx.clock.now()), reason.strip(), draft_id))
    conn.commit()
    finalize_newsletter(conn, ctx, d["newsletter_id"])


# ------------------------------------------------------------------ run execution
def _generate_for(conn: Conn, ctx: Context, newsletter_id: str, sub: dict, sched: dict) -> str | None:
    """Pipeline for one recipient. Returns draft id. Isolated in a savepoint; failures become 'failed' drafts."""
    draft_id = new_id()
    conn.run("INSERT INTO draft_emails (id, newsletter_id, subscriber_id, recipient_email, language, status, created_at) "
             "VALUES (?,?,?,?,?,'generating',?)", (draft_id, newsletter_id, sub["id"], sub["email"], sub["language"], iso(ctx.clock.now())))
    conn.commit()
    state = DraftState(newsletter_id, draft_id, sub, _prefs(conn, sub["id"]), json.loads(sched["topics_json"]),
                       json.loads(sched["source_keys_json"]))
    try:
        with conn.savepoint():
            run_graph(state, conn, ctx)
    except PipelineError as exc:
        conn.run("UPDATE draft_emails SET status='failed', error=?, error_stage=? WHERE id=?",
                 (str(exc)[:900], exc.stage, draft_id))
        flush_audit(conn, ctx, state)
        conn.commit()
        return draft_id
    except Exception as exc:  # noqa: BLE001
        conn.run("UPDATE draft_emails SET status='failed', error=?, error_stage='pipeline' WHERE id=?",
                 (f"{type(exc).__name__}: {exc}"[:900], draft_id))
        flush_audit(conn, ctx, state)
        conn.commit()
        return draft_id
    if state.decision == "skip":
        status = "skipped"
    elif state.decision == "review":
        status = "pending_review"
    else:
        status = "draft"
    conn.run("UPDATE draft_emails SET status=?, subject=?, html_body=?, text_body=?, relevance=?, risk=?, decision=?, "
             "decision_reason=? WHERE id=?",
             (status, state.subject or None, state.html or None, state.text or None, state.relevance, state.risk,
              state.decision, state.reason, draft_id))
    flush_audit(conn, ctx, state)
    conn.commit()
    return draft_id


def execute_run(conn: Conn, ctx: Context, sched: dict, run_id: str) -> tuple[str, str]:
    newsletter_id = new_id()
    conn.run("INSERT INTO newsletters (id, schedule_run_id, status, topics_json, created_at) VALUES (?,?,?,?,?)",
             (newsletter_id, run_id, "running", sched["topics_json"], iso(ctx.clock.now())))
    conn.run("UPDATE schedule_runs SET newsletter_id=? WHERE id=?", (newsletter_id, run_id))
    conn.commit()
    try:
        ingest.ensure_fresh_ingest(conn, ctx.settings, ctx.clock)
    except Exception as exc:  # noqa: BLE001 - stale articles are better than no newsletter
        conn.rollback()
        log.warning("ingest before run failed, continuing with stored articles: %s", exc)
    subs = conn.all("SELECT * FROM subscribers WHERE is_active = 1 ORDER BY email")
    wanted = set(json.loads(sched["topics_json"]))
    for sub in subs:
        topics = {r["topic_key"] for r in conn.all("SELECT topic_key FROM subscriber_topics WHERE subscriber_id=?", (sub["id"],))}
        if wanted and not (topics & wanted):
            continue
        if not topics:
            continue
        draft_id = _generate_for(conn, ctx, newsletter_id, sub, sched)
        if conn.scalar("SELECT status FROM draft_emails WHERE id=?", (draft_id,)) == "draft":
            send_draft(conn, ctx, draft_id)   # commits itself
    return finalize_newsletter(conn, ctx, newsletter_id)


def claim_run(conn: Conn, ctx: Context, schedule_id: str, run_key: str, scheduled_for: datetime | None) -> str | None:
    """Insert + commit the unique run row BEFORE doing any work. Returns run id, or None if someone already claimed it."""
    rid = new_id()
    n = conn.run("INSERT INTO schedule_runs (id, schedule_id, run_key, scheduled_for, status, message, started_at) "
                 "VALUES (?,?,?,?,'running','started',?) ON CONFLICT (run_key) DO NOTHING",
                 (rid, schedule_id, run_key, iso(scheduled_for) if scheduled_for else None, iso(ctx.clock.now())))
    conn.commit()
    return rid if n else None


def process_run(conn: Conn, ctx: Context, sched: dict, run_id: str) -> str:
    try:
        status, _ = execute_run(conn, ctx, sched, run_id)
        return status
    except Exception as exc:  # noqa: BLE001 - persist the failure, never swallow it
        conn.rollback()
        stage = getattr(exc, "stage", "run")
        conn.run("UPDATE schedule_runs SET status='failed', message=?, completed_at=? WHERE id=?",
                 (f"{stage}: {type(exc).__name__}: {exc}"[:1500], iso(ctx.clock.now()), run_id))
        conn.commit()
        log.exception("run %s crashed", run_id)
        return "failed"


def run_schedule_now(conn: Conn, ctx: Context, schedule_id: str) -> tuple[str, str]:
    sched = conn.one("SELECT * FROM newsletter_schedules WHERE id = ?", (schedule_id,))
    if not sched:
        raise ValueError("unknown schedule")
    rid = claim_run(conn, ctx, schedule_id, f"manual:{schedule_id}:{new_id()}", None)
    process_run(conn, ctx, sched, rid)
    row = conn.one("SELECT status, message FROM schedule_runs WHERE id = ?", (rid,))
    return row["status"], row["message"]


def _record_invalid(conn: Conn, ctx: Context, sched: dict, err: str) -> None:
    day = ctx.clock.now().strftime("%Y-%m-%d")
    rid = claim_run(conn, ctx, sched["id"], f"invalid:{sched['id']}:{day}", None)
    if rid:
        conn.run("UPDATE schedule_runs SET status='failed', message=?, completed_at=? WHERE id=?",
                 (f"schedule is malformed: {err}", iso(ctx.clock.now()), rid))
        conn.commit()


def scan_once(conn: Conn, ctx: Context) -> dict:
    now = ctx.clock.now()
    summary = {"schedules": 0, "due": 0, "claimed": 0, "already_done": 0, "invalid": 0, "errors": 0, "statuses": {}}
    for sched in conn.all("SELECT * FROM newsletter_schedules WHERE enabled = 1 ORDER BY created_at, id"):
        summary["schedules"] += 1
        try:
            try:
                moments = due_moments(sched, now, ctx.settings.schedule_catchup_minutes)
            except ScheduleError as exc:
                summary["invalid"] += 1
                _record_invalid(conn, ctx, sched, str(exc))
                continue
            for m in moments:
                summary["due"] += 1
                rid = claim_run(conn, ctx, sched["id"], f"{sched['id']}:{iso(m)}", m)
                if rid is None:
                    summary["already_done"] += 1
                    continue
                summary["claimed"] += 1
                status = process_run(conn, ctx, sched, rid)
                summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
        except Exception:  # noqa: BLE001 - one bad schedule must never abort the scan
            conn.rollback()
            summary["errors"] += 1
            log.exception("schedule %s: unexpected error", sched.get("id"))
    conn.run("INSERT INTO automation_state (state_key, value, updated_at) VALUES ('worker_heartbeat', ?, ?) "
             "ON CONFLICT (state_key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
             (iso(now), iso(now)))
    conn.commit()
    return summary
