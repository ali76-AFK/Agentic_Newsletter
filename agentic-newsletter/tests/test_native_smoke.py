"""Runs the real smoke scenario (app.smoke) against the REAL worker process, with FakeSMTP/FakeMailpit standing in for Mailpit."""
import os
import subprocess
import sys
import time
import unittest

from app import ingest, topics
from tests.fake_mailpit import FakeMailpit
from tests.fake_smtp import FakeSMTP
from tests.helpers import ROOT, TempDB


class NativeSmoke(unittest.TestCase):
    def test_smoke_passes_against_real_worker_process(self):
        tmp = TempDB(); self.addCleanup(tmp.cleanup)
        smtp = FakeSMTP().start(); self.addCleanup(smtp.stop)
        mp = FakeMailpit(smtp).start(); self.addCleanup(mp.stop)
        conn = tmp.db.connect()
        topics.seed_topics(conn); ingest.seed_sources(conn, os.path.join(ROOT, "sources.yaml")); conn.commit(); conn.close()
        env = dict(os.environ, DATABASE_URL=tmp.url, SECRET_KEY="smoke-secret", TIMEZONE="Europe/Berlin", FEED_MODE="fixture",
                   EMAIL_MODE="mailpit", MAILPIT_SMTP_HOST="127.0.0.1", MAILPIT_SMTP_PORT=str(smtp.port),
                   MAILPIT_API_URL=f"http://127.0.0.1:{mp.port}", SCHEDULE_SCAN_SECONDS="2", SCHEDULE_CATCHUP_MINUTES="15",
                   SOURCES_FILE=os.path.join(ROOT, "sources.yaml"), PYTHONPATH=ROOT)
        worker = subprocess.Popen([sys.executable, "-m", "app.worker"], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            time.sleep(1.5)
            out = subprocess.run([sys.executable, "-m", "app.smoke"], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
        finally:
            worker.terminate()
            try:
                wlog = worker.communicate(timeout=10)[0].decode()
            except subprocess.TimeoutExpired:
                worker.kill(); wlog = "(worker did not stop on SIGTERM!)"
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr + "\n--- worker log ---\n" + wlog)
        self.assertIn("RESULT: PASS", out.stdout)
        self.assertIn("worker stopped", wlog, "worker must shut down gracefully on SIGTERM")


if __name__ == "__main__":
    unittest.main()
