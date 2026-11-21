"""Shared test fixtures. Tests use an EPHEMERAL database and an injected clock - never the developer database.

* inside Docker (TEST_DATABASE_ADMIN_URL set): a throw-away PostgreSQL database is created and dropped per test class
* otherwise: a throw-away SQLite file
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone

from app import ingest, topics
from app.clock import FixedClock
from app.config import Settings
from app.db import Database, apply_migrations_sql
from app.llm import MockProvider
from app.mailer import Mailer, SmtpConfig
from app.service import Context, make_context

from .fake_smtp import FakeSMTP

NOW = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)   # injected clock start (a Wednesday); fixtures are relative to it
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TempDB:
    def __init__(self):
        admin = os.environ.get("TEST_DATABASE_ADMIN_URL")
        self.admin_url = admin
        if admin:
            import psycopg
            self.name = "test_" + uuid.uuid4().hex[:12]
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute(f"CREATE DATABASE {self.name}")
            base = admin.rsplit("/", 1)[0]
            self.url = f"{base}/{self.name}"
            self.dir = None
        else:
            self.dir = tempfile.mkdtemp(prefix="nl-test-")
            self.url = "sqlite:///" + os.path.join(self.dir, "t.db")
        self.db = Database(self.url)
        c = self.db.connect()
        apply_migrations_sql(c)
        c.close()

    def cleanup(self):
        if self.admin_url:
            import psycopg
            with psycopg.connect(self.admin_url, autocommit=True) as c:
                c.execute(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)")
        elif self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


class BaseCase(unittest.TestCase):
    """DB + fixed clock + fake SMTP (plays the role of Mailpit) + seeded topics/sources + ingested fixtures."""
    ingest_fixtures = True

    def setUp(self):
        self.tmp = TempDB()
        self.addCleanup(self.tmp.cleanup)
        self.conn = self.tmp.db.connect()
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(NOW)
        self.smtp = FakeSMTP().start()
        self.addCleanup(self.smtp.stop)
        self.settings = Settings(database_url=self.tmp.url, secret_key="test-secret-key", timezone="Europe/Berlin",
                                 mailpit_smtp_host="127.0.0.1", mailpit_smtp_port=self.smtp.port,
                                 sources_file=os.path.join(ROOT, "sources.yaml"), schedule_catchup_minutes=15)
        topics.seed_topics(self.conn)
        ingest.seed_sources(self.conn, self.settings.sources_file)
        self.conn.commit()
        if self.ingest_fixtures:
            ingest.ingest_all(self.conn, self.settings, self.clock, fixture_tag="base")
        self.ctx = make_context(self.settings, clock=self.clock, llm=MockProvider(), sleep=lambda s: None)

    # helpers -----------------------------------------------------------------
    def ctx_with(self, **kw) -> Context:
        return make_context(self.settings.with_(**kw), clock=self.clock, llm=kw.pop("llm", None) or MockProvider(),
                            sleep=lambda s: None)

    def sub(self, email, topics_=("ai_news",), language="en", **kw):
        from app.service import add_subscriber
        return add_subscriber(self.conn, self.clock, email, email.split("@")[0].title(), language, list(topics_), **kw)

    def schedule(self, topics_=(), **kw):
        from app.service import create_schedule
        args = dict(name="t", delivery_time="11:00", timezone="Europe/Berlin", topics=list(topics_))
        args.update(kw)
        return create_schedule(self.conn, self.clock, **args)

    def runs(self, schedule_id=None):
        q, p = "SELECT * FROM schedule_runs", ()
        if schedule_id:
            q, p = q + " WHERE schedule_id = ?", (schedule_id,)
        return self.conn.all(q + " ORDER BY started_at, run_key", p)

    def drafts(self, **where):
        rows = self.conn.all("SELECT * FROM draft_emails ORDER BY recipient_email")
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def to_addresses(self):
        return [m["to"] for m in self.smtp.messages]
