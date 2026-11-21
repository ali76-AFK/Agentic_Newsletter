"""Web UI tests: Streamlit's headless AppTest against a stub API (no database, no network)."""
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, UI_DIR)

from streamlit.testing.v1 import AppTest  # noqa: E402

import common  # noqa: E402

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
CONFIG = {"timezone": "Europe/Berlin", "email_mode": "mailpit", "feed_mode": "fixture", "llm_provider": "mock",
          "default_language": "en", "schedule_scan_seconds": 30, "feed_freshness_days": 30, "smtp_password": "not set"}
DATA = {
    ("GET", "/api/diagnostics"): {"config": CONFIG, "worker_heartbeat": NOW},
    ("GET", "/api/topics"): [{"topic_key": "ai_news", "label": "AI news", "is_active": 1}, {"topic_key": "devops", "label": "DevOps", "is_active": 1}],
    ("GET", "/api/sources"): [{"source_key": "fx", "name": "Fixture", "feed_url": "fixture://fx", "language": "en", "mode": "fixture", "quality": "high", "enabled": 1}],
    ("GET", "/api/subscribers"): [{"id": "s1", "email": "a@x.test", "name": "Ann", "language": "en", "is_active": 1, "created_at": NOW,
                                   "unsubscribed_at": None, "topics": ["ai_news"]}],
    ("GET", "/api/subscribers/s1/preferences"): {"subscriber_id": "s1", "max_articles": 5, "lookback_hours": 168, "summary_style": "concise",
                                                 "excluded_keywords_json": "[]", "source_keys_json": "[]"},
    ("GET", "/api/schedules"): [{"id": "c1", "name": "Morning", "topics_json": "[]", "source_keys_json": "[]", "weekdays_json": "[0, 1, 2, 3, 4, 5, 6]",
                                 "delivery_time": "07:30", "timezone": "Europe/Berlin", "enabled": 1, "created_at": NOW}],
    ("GET", "/api/schedules/c1/runs"): [{"id": "r1", "run_key": "manual:c1:x", "scheduled_for": None, "status": "sent", "message": "1 sent",
                                         "newsletter_id": "n1", "started_at": NOW, "completed_at": NOW}],
    ("GET", "/api/runs"): [{"id": "r1", "schedule_id": "c1", "run_key": "manual:c1:x", "scheduled_for": None, "status": "sent",
                            "message": "1 sent", "started_at": NOW, "completed_at": NOW}],
    ("GET", "/api/editions"): [{"id": "n1", "schedule_run_id": "r1", "status": "sent", "created_at": NOW}],
    ("GET", "/api/editions/n1"): {"id": "n1", "status": "sent", "drafts": [
        {"id": "d1", "recipient_email": "a@x.test", "status": "failed", "relevance": 0.9, "risk": "low", "decision": "auto_send",
         "decision_reason": "ok", "error": "boom", "error_stage": "send", "sent_at": None}]},
    ("GET", "/api/review"): [{"id": "d9", "newsletter_id": "n1", "recipient_email": "a@x.test", "subject": "Held", "relevance": 0.7,
                              "risk": "medium", "decision_reason": "medium risk", "text_body": "Hello", "created_at": NOW}],
    ("POST", "/api/ingest"): {"sources": 1, "fetched": 4, "new": 3, "duplicate": 1, "too_old": 0, "errors": {}},
    ("POST", "/api/subscribers"): {"id": "new"},
    ("POST", "/api/review/d9/approve"): {"status": "sent"},
    ("POST", "/api/review/d9/reject"): {"status": "rejected"},
    ("POST", "/api/schedules/c1/run-now"): {"status": "sent", "message": "1 sent"},
    ("POST", "/api/editions/n1/retry"): {"status": "sent", "message": "retried"},
    ("PATCH", "/api/subscribers/s1"): {"ok": True},
}


class Stub:
    def __init__(self, data):
        self.data, self.calls = dict(data), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                path = self.path.split("?")[0]
                outer.calls.append((self.command, path, body))
                hit = outer.data.get((self.command, path))
                code, payload = (200, hit) if hit is not None else (404, {"detail": f"no stub for {self.command} {path}"})
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


PAGES = ["page_overview.py", "page_subscribers.py", "page_schedules.py", "page_review.py", "page_history.py", "page_sources.py", "page_system.py"]


def run_page(name, url):
    os.environ["UI_API_URL"] = url
    return AppTest.from_file(os.path.join(UI_DIR, name), default_timeout=30).run()


class PageTests(unittest.TestCase):
    def setUp(self):
        self.stub = Stub(DATA)

    def tearDown(self):
        self.stub.stop()

    def test_every_page_renders_with_data(self):
        for p in PAGES:
            at = run_page(p, self.stub.url)
            self.assertFalse(at.exception, f"{p}: {[e.value for e in at.exception]}")
            unexpected = [e.value for e in at.error if "failed to send" not in e.value]   # the stub has one failed email on purpose
            self.assertFalse(unexpected, f"{p}: {unexpected}")

    def test_full_app_shell_renders_with_review_link(self):
        os.environ["UI_API_URL"] = self.stub.url
        at = AppTest.from_file(os.path.join(UI_DIR, "app.py"), default_timeout=30).run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual([t.value for t in at.title], ["📊 Overview"])
        self.assertIn("1", [m.value for m in at.metric if m.label == "Waiting for review"])

    def test_every_page_renders_with_empty_data(self):
        empty = {k: ([] if isinstance(v, list) else v) for k, v in DATA.items()}
        stub = Stub(empty)
        try:
            for p in PAGES:
                at = run_page(p, stub.url)
                self.assertFalse(at.exception, f"{p}: {[e.value for e in at.exception]}")
        finally:
            stub.stop()

    def test_api_down_shows_a_message_not_a_crash(self):
        for p in PAGES:
            at = run_page(p, "http://127.0.0.1:9")   # nothing listens on port 9
            self.assertFalse(at.exception, p)
            self.assertTrue(any("cannot reach the API" in e.value for e in at.error), p)

    def test_stale_worker_is_flagged(self):
        data = dict(DATA)
        data[("GET", "/api/diagnostics")] = {"config": CONFIG, "worker_heartbeat": "2020-01-01T00:00:00Z"}
        stub = Stub(data)
        try:
            at = run_page("page_overview.py", stub.url)
            self.assertTrue(any("worker has not reported" in e.value for e in at.error))
        finally:
            stub.stop()

    def test_add_subscriber_posts_the_form_values(self):
        at = run_page("page_subscribers.py", self.stub.url)
        at.text_input[0].set_value("  New@X.test ")
        at.text_input[1].set_value("New")
        at.multiselect[0].set_value(["devops"])
        at.button[0].click().run()
        self.assertIn(("POST", "/api/subscribers", {"email": "  New@X.test ", "name": "New", "language": "en", "topics": ["devops"]}),
                      self.stub.writes())
        self.assertTrue(any("Added" in s.value for s in at.success))

    def test_add_subscriber_requires_email_and_name(self):
        at = run_page("page_subscribers.py", self.stub.url)
        at.button[0].click().run()
        self.assertTrue(at.error)
        self.assertEqual(self.stub.writes(), [])

    def test_api_error_text_is_shown(self):
        data = dict(DATA)
        data.pop(("POST", "/api/subscribers"))   # stub answers 404 with a detail message
        stub = Stub(data)
        try:
            at = run_page("page_subscribers.py", stub.url)
            at.text_input[0].set_value("a@x.test"); at.text_input[1].set_value("A")
            at.button[0].click().run()
            self.assertTrue(any("no stub for POST" in e.value for e in at.error))
        finally:
            stub.stop()

    def test_review_approve_and_reject(self):
        at = run_page("page_review.py", self.stub.url)
        [b for b in at.button if "Approve" in b.label][0].click().run()
        self.assertIn(("POST", "/api/review/d9/approve", None), self.stub.writes())
        at = run_page("page_review.py", self.stub.url)
        [b for b in at.button if "Reject" in b.label][0].click().run()
        self.assertTrue(any("reason" in e.value for e in at.error))
        self.assertFalse([w for w in self.stub.writes() if w[1].endswith("/reject")])
        at.text_input[0].set_value("not relevant")
        [b for b in at.button if "Reject" in b.label][0].click().run()
        self.assertIn(("POST", "/api/review/d9/reject", {"reason": "not relevant"}), self.stub.writes())

    def test_real_email_mode_needs_a_tick_before_sending(self):
        data = dict(DATA)
        data[("GET", "/api/diagnostics")] = {"config": {**CONFIG, "email_mode": "smtp"}, "worker_heartbeat": NOW}
        stub = Stub(data)
        try:
            for page, label in (("page_review.py", "Approve"), ("page_schedules.py", "Run now")):
                at = run_page(page, stub.url)
                btn = [b for b in at.button if label in b.label][0]
                self.assertTrue(btn.disabled, f"{page}: {label} must be disabled until the box is ticked")
                at.checkbox[0].check().run()
                self.assertFalse([b for b in at.button if label in b.label][0].disabled)
        finally:
            stub.stop()

    def test_run_now_and_retry_and_fetch(self):
        at = run_page("page_schedules.py", self.stub.url)
        [b for b in at.button if "Run now" in b.label][0].click().run()
        self.assertIn(("POST", "/api/schedules/c1/run-now", None), self.stub.writes())
        at = run_page("page_history.py", self.stub.url)
        [b for b in at.button if "Retry" in b.label][0].click().run()
        self.assertIn(("POST", "/api/editions/n1/retry", None), self.stub.writes())
        at = run_page("page_sources.py", self.stub.url)
        [b for b in at.button if "Fetch" in b.label][0].click().run()
        self.assertEqual([m.value for m in at.metric][:3], ["1", "4", "3"])


class HelperTests(unittest.TestCase):
    def test_next_run_respects_weekdays_and_timezone(self):
        # Wednesday 2026-03-04 10:00 UTC = 11:00 Berlin. A 07:30 schedule on Mon/Wed -> next is Monday 07:30 Berlin = 06:30 UTC.
        now = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
        s = {"timezone": "Europe/Berlin", "delivery_time": "07:30", "weekdays_json": "[0, 2]"}
        self.assertEqual(common.next_run(s, now), datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc))
        # later today still counts
        s2 = {"timezone": "Europe/Berlin", "delivery_time": "18:00", "weekdays_json": "[2]"}
        self.assertEqual(common.next_run(s2, now), datetime(2026, 3, 4, 17, 0, tzinfo=timezone.utc))

    def test_next_run_bad_data_is_none(self):
        self.assertIsNone(common.next_run({"timezone": "Nope/Zone", "delivery_time": "07:30", "weekdays_json": "[1]"}, datetime.now(timezone.utc)))
        self.assertIsNone(common.next_run({"timezone": "UTC", "delivery_time": "x", "weekdays_json": "[1]"}, datetime.now(timezone.utc)))

    def test_worker_state(self):
        now = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
        ok, _ = common.worker_state({"worker_heartbeat": "2026-03-04T09:59:30Z", "config": {"schedule_scan_seconds": 30}}, now)
        self.assertTrue(ok)
        bad, text = common.worker_state({"worker_heartbeat": "2026-03-04T09:50:00Z", "config": {"schedule_scan_seconds": 30}}, now)
        self.assertFalse(bad)
        self.assertFalse(common.worker_state({"worker_heartbeat": None, "config": {}}, now)[0])

    def test_validation_errors_are_readable(self):
        class R:
            status_code = 422
            text = ""
            def json(self):
                return {"detail": [{"loc": ["body", "email"], "msg": "field required"}]}
        self.assertEqual(common._detail(R()), "email: field required")

    def test_weekdays_text_and_jlist(self):
        self.assertEqual(common.weekdays_text(list(range(7))), "every day")
        self.assertEqual(common.weekdays_text([0, 1, 2, 3, 4]), "Mon-Fri")
        self.assertEqual(common.weekdays_text([5, 0]), "Mon, Sat")
        self.assertEqual(common.jlist("not json"), [])


if __name__ == "__main__":
    unittest.main()
