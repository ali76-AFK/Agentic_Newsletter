"""FastAPI service. A thin layer over app.service (which is what the tests exercise)."""
from __future__ import annotations

import json
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import ingest, service
from .clock import SystemClock
from .config import Settings
from .db import Database
from .email_build import verify_token
from .mailer import MailError, build_mailer
from .scheduling import ScheduleError

app = FastAPI(title="Agentic Newsletter API", version="1.0", docs_url="/docs", openapi_url="/openapi.json")


@lru_cache
def settings() -> Settings:
    return Settings.from_env()


@lru_cache
def context() -> service.Context:
    return service.make_context(settings())


def get_conn():
    conn = Database(settings().database_url).connect()
    try:
        yield conn
    finally:
        conn.close()


class SubscriberIn(BaseModel):
    email: str
    name: str
    language: str = "en"
    topics: list[str] = Field(default_factory=list)


class SubscriberPatch(BaseModel):
    name: str | None = None
    language: str | None = None
    topics: list[str] | None = None
    is_active: bool | None = None


class PreferencesIn(BaseModel):
    max_articles: int = Field(5, ge=1, le=20)
    lookback_hours: int = Field(168, ge=1, le=24 * 60)
    summary_style: str = "concise"
    excluded_keywords: list[str] = Field(default_factory=list)
    source_keys: list[str] = Field(default_factory=list)


class ScheduleIn(BaseModel):
    name: str
    delivery_time: str
    timezone: str
    topics: list[str] = Field(default_factory=list)
    source_keys: list[str] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)))
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    delivery_time: str | None = None
    timezone: str | None = None
    topics: list[str] | None = None
    source_keys: list[str] | None = None
    weekdays: list[int] | None = None
    enabled: bool | None = None


class RejectIn(BaseModel):
    reason: str


@app.get("/health")
def health(conn=Depends(get_conn)):
    conn.scalar("SELECT 1")
    return {"status": "ok"}


@app.get("/api/topics")
def list_topics(conn=Depends(get_conn)):
    return conn.all("SELECT topic_key, label, is_active FROM topics ORDER BY topic_key")


@app.get("/api/sources")
def list_sources(conn=Depends(get_conn)):
    return conn.all("SELECT source_key, name, feed_url, language, mode, quality, enabled FROM sources ORDER BY source_key")


@app.post("/api/subscribers", status_code=201)
def create_subscriber(body: SubscriberIn, conn=Depends(get_conn)):
    try:
        sid = service.add_subscriber(conn, SystemClock(), body.email, body.name, body.language, body.topics)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(409, f"could not create subscriber (duplicate email?): {exc}")
    return {"id": sid}


@app.get("/api/subscribers")
def list_subscribers(conn=Depends(get_conn)):
    rows = conn.all("SELECT id, email, name, language, is_active, created_at, unsubscribed_at FROM subscribers ORDER BY email")
    for r in rows:
        r["topics"] = [t["topic_key"] for t in conn.all("SELECT topic_key FROM subscriber_topics WHERE subscriber_id = ?", (r["id"],))]
    return rows


@app.get("/api/subscribers/{subscriber_id}")
def get_subscriber(subscriber_id: str, conn=Depends(get_conn)):
    r = conn.one("SELECT id, email, name, language, is_active, created_at, unsubscribed_at FROM subscribers WHERE id = ?", (subscriber_id,))
    if not r:
        raise HTTPException(404, "subscriber not found")
    r["topics"] = [t["topic_key"] for t in conn.all("SELECT topic_key FROM subscriber_topics WHERE subscriber_id = ?", (subscriber_id,))]
    return r


@app.patch("/api/subscribers/{subscriber_id}")
def patch_subscriber(subscriber_id: str, body: SubscriberPatch, conn=Depends(get_conn)):
    if not conn.one("SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)):
        raise HTTPException(404, "subscriber not found")
    if body.language is not None and body.language not in ("en", "de"):
        raise HTTPException(422, "language must be 'en' or 'de'")
    if body.name is not None:
        conn.run("UPDATE subscribers SET name = ? WHERE id = ?", (body.name, subscriber_id))
    if body.language is not None:
        conn.run("UPDATE subscribers SET language = ? WHERE id = ?", (body.language, subscriber_id))
    if body.is_active is not None:
        conn.run("UPDATE subscribers SET is_active = ? WHERE id = ?", (1 if body.is_active else 0, subscriber_id))
    if body.topics is not None:
        known = {r["topic_key"] for r in conn.all("SELECT topic_key FROM topics")}
        bad = [t for t in body.topics if t not in known]
        if bad:
            conn.rollback()
            raise HTTPException(422, f"unknown topics: {bad}")
        conn.run("DELETE FROM subscriber_topics WHERE subscriber_id = ?", (subscriber_id,))
        for t in body.topics:
            conn.run("INSERT INTO subscriber_topics (subscriber_id, topic_key) VALUES (?,?)", (subscriber_id, t))
    conn.commit()
    return {"ok": True}


@app.delete("/api/subscribers/{subscriber_id}")
def delete_subscriber(subscriber_id: str, conn=Depends(get_conn)):
    """Soft delete: deactivates the subscriber (history stays intact)."""
    if not service.unsubscribe(conn, SystemClock(), subscriber_id):
        raise HTTPException(404, "subscriber not found or already inactive")
    return {"ok": True}


@app.get("/api/subscribers/{subscriber_id}/preferences")
def get_preferences(subscriber_id: str, conn=Depends(get_conn)):
    r = conn.one("SELECT * FROM subscription_preferences WHERE subscriber_id = ?", (subscriber_id,))
    if not r:
        raise HTTPException(404, "preferences not found")
    return r


@app.put("/api/subscribers/{subscriber_id}/preferences")
def put_preferences(subscriber_id: str, body: PreferencesIn, conn=Depends(get_conn)):
    n = conn.run("UPDATE subscription_preferences SET max_articles=?, lookback_hours=?, summary_style=?, excluded_keywords_json=?, "
                 "source_keys_json=? WHERE subscriber_id=?",
                 (body.max_articles, body.lookback_hours, body.summary_style, json.dumps(body.excluded_keywords),
                  json.dumps(body.source_keys), subscriber_id))
    conn.commit()
    if not n:
        raise HTTPException(404, "preferences not found")
    return {"ok": True}


@app.post("/api/ingest")
def trigger_ingest(conn=Depends(get_conn)):
    return ingest.ingest_all(conn, settings(), SystemClock())


@app.get("/api/editions")
def list_editions(conn=Depends(get_conn)):
    return conn.all("SELECT id, schedule_run_id, status, created_at FROM newsletters ORDER BY created_at DESC LIMIT 100")


@app.get("/api/editions/{newsletter_id}")
def get_edition(newsletter_id: str, conn=Depends(get_conn)):
    n = conn.one("SELECT * FROM newsletters WHERE id = ?", (newsletter_id,))
    if not n:
        raise HTTPException(404, "edition not found")
    n["drafts"] = conn.all("SELECT id, recipient_email, status, relevance, risk, decision, decision_reason, error, error_stage, "
                           "sent_at FROM draft_emails WHERE newsletter_id = ? ORDER BY recipient_email", (newsletter_id,))
    return n


@app.post("/api/editions/{newsletter_id}/retry")
def retry_edition(newsletter_id: str, conn=Depends(get_conn)):
    status, message = service.retry_failed(conn, context(), newsletter_id)
    return {"status": status, "message": message}


@app.get("/api/review")
def review_queue(conn=Depends(get_conn)):
    return conn.all("SELECT id, newsletter_id, recipient_email, subject, relevance, risk, decision_reason, text_body, created_at "
                    "FROM draft_emails WHERE status = 'pending_review' ORDER BY created_at")


@app.post("/api/review/{draft_id}/approve")
def approve(draft_id: str, conn=Depends(get_conn)):
    try:
        return {"status": service.approve_draft(conn, context(), draft_id)}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/review/{draft_id}/reject")
def reject(draft_id: str, body: RejectIn, conn=Depends(get_conn)):
    try:
        service.reject_draft(conn, context(), draft_id, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "rejected"}


@app.get("/api/schedules")
def list_schedules(conn=Depends(get_conn)):
    return conn.all("SELECT * FROM newsletter_schedules ORDER BY created_at")


@app.post("/api/schedules", status_code=201)
def create_schedule(body: ScheduleIn, conn=Depends(get_conn)):
    try:
        sid = service.create_schedule(conn, SystemClock(), body.name, body.delivery_time, body.timezone, body.topics,
                                      body.source_keys, body.weekdays, body.enabled)
    except ScheduleError as exc:
        raise HTTPException(422, str(exc))
    return {"id": sid}


@app.patch("/api/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: SchedulePatch, conn=Depends(get_conn)):
    cur = conn.one("SELECT * FROM newsletter_schedules WHERE id = ?", (schedule_id,))
    if not cur:
        raise HTTPException(404, "schedule not found")
    from .scheduling import parse_schedule
    new = dict(cur)
    if body.name is not None: new["name"] = body.name
    if body.delivery_time is not None: new["delivery_time"] = body.delivery_time
    if body.timezone is not None: new["timezone"] = body.timezone
    if body.topics is not None: new["topics_json"] = json.dumps(body.topics)
    if body.source_keys is not None: new["source_keys_json"] = json.dumps(body.source_keys)
    if body.weekdays is not None: new["weekdays_json"] = json.dumps(body.weekdays)
    if body.enabled is not None: new["enabled"] = 1 if body.enabled else 0
    try:
        parse_schedule(new)
    except ScheduleError as exc:
        raise HTTPException(422, str(exc))
    conn.run("UPDATE newsletter_schedules SET name=?, delivery_time=?, timezone=?, topics_json=?, source_keys_json=?, weekdays_json=?, "
             "enabled=? WHERE id=?", (new["name"], new["delivery_time"], new["timezone"], new["topics_json"],
                                      new["source_keys_json"], new["weekdays_json"], new["enabled"], schedule_id))
    conn.commit()
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, conn=Depends(get_conn)):
    """Disables the schedule (run history is kept)."""
    n = conn.run("UPDATE newsletter_schedules SET enabled = 0 WHERE id = ?", (schedule_id,))
    conn.commit()
    if not n:
        raise HTTPException(404, "schedule not found")
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run-now")
def run_now(schedule_id: str, conn=Depends(get_conn)):
    try:
        status, message = service.run_schedule_now(conn, context(), schedule_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"status": status, "message": message}


@app.get("/api/schedules/{schedule_id}/runs")
def schedule_runs(schedule_id: str, conn=Depends(get_conn)):
    return conn.all("SELECT id, run_key, scheduled_for, status, message, newsletter_id, started_at, completed_at FROM schedule_runs "
                    "WHERE schedule_id = ? ORDER BY started_at DESC LIMIT 200", (schedule_id,))


@app.get("/api/runs")
def all_runs(conn=Depends(get_conn)):
    return conn.all("SELECT id, schedule_id, run_key, scheduled_for, status, message, started_at, completed_at FROM schedule_runs "
                    "ORDER BY started_at DESC LIMIT 200")


@app.get("/api/diagnostics")
def diagnostics(conn=Depends(get_conn)):
    return {"config": settings().redacted(), "worker_heartbeat": conn.scalar(
        "SELECT value FROM automation_state WHERE state_key = 'worker_heartbeat'")}


@app.post("/api/diagnostics/smtp-check")
def smtp_check():
    mailer, _ = build_mailer(settings())
    try:
        return {"ok": True, "info": mailer.check_connection()}
    except MailError as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, conn=Depends(get_conn)):
    sid = verify_token(settings().secret_key, token)
    if not sid:
        raise HTTPException(400, "invalid unsubscribe link")
    return HTMLResponse(f"<html><body><h2>Unsubscribe</h2><form method='post'><button type='submit'>Confirm unsubscribe</button></form></body></html>")


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, conn=Depends(get_conn)):
    sid = verify_token(settings().secret_key, token)
    if not sid:
        raise HTTPException(400, "invalid unsubscribe link")
    service.unsubscribe(conn, SystemClock(), sid)
    return HTMLResponse("<html><body><h2>You have been unsubscribed.</h2></body></html>")
