import socket
import unittest
from datetime import datetime, timedelta, timezone

from app import feeds, ingest
from app.clock import FixedClock
from app.feeds import FeedError, is_public_ip, parse_feed, validate_feed_url
from tests.helpers import BaseCase, NOW

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Hello &amp; <b>World</b></title><link>https://e.test/1</link><guid>g1</guid>
<pubDate>Wed, 04 Mar 2026 08:00:00 +0000</pubDate><description>&lt;p&gt;Body &lt;script&gt;evil()&lt;/script&gt;text&lt;/p&gt;</description></item>
<item><title>No date</title><link>https://e.test/2</link><guid>g2</guid></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom one</title>
<link href="https://e.test/a1"/><id>urn:a1</id><updated>2026-03-04T08:00:00Z</updated><summary>Atom body.</summary></entry></feed>"""


class Parsing(unittest.TestCase):
    def test_rss(self):
        e = parse_feed(RSS)
        self.assertEqual(len(e), 1)                      # entry without a date is dropped
        self.assertEqual(e[0].title, "Hello & World")
        self.assertNotIn("evil", e[0].body)
        self.assertEqual(e[0].published_at, datetime(2026, 3, 4, 8, tzinfo=timezone.utc))

    def test_atom(self):
        e = parse_feed(ATOM)
        self.assertEqual((e[0].url, e[0].external_id), ("https://e.test/a1", "urn:a1"))

    def test_invalid_xml(self):
        with self.assertRaises(FeedError):
            parse_feed("<rss><item>")

    def test_strip_html(self):
        self.assertEqual(feeds.strip_html("<p>a&nbsp;<i>b</i></p><style>x{}</style>"), "a b")


class Fixtures(unittest.TestCase):
    def test_dates_are_relative_to_now_not_hardcoded(self):
        for now in (NOW, NOW + timedelta(days=900)):
            es = parse_feed(feeds.fixture_xml("fixture-tech-en", now, "t"))
            self.assertTrue(es)
            for e in es:
                self.assertLess(now - e.published_at, timedelta(days=1))
                self.assertGreater(now - e.published_at, timedelta(0))

    def test_tag_makes_content_unique(self):
        a = parse_feed(feeds.fixture_xml("fixture-tech-en", NOW, "a"))[0]
        b = parse_feed(feeds.fixture_xml("fixture-tech-en", NOW, "b"))[0]
        self.assertNotEqual((a.url, a.external_id, a.body), (b.url, b.external_id, b.body))

    def test_unknown_fixture(self):
        with self.assertRaises(FeedError):
            feeds.fixture_xml("nope", NOW, "t")


class Ssrf(unittest.TestCase):
    def test_ip_classification(self):
        for ip in ("10.0.0.1", "192.168.1.5", "172.16.0.1", "127.0.0.1", "169.254.169.254", "::1", "0.0.0.0", "fe80::1", "fc00::1"):
            self.assertFalse(is_public_ip(ip), ip)
        for ip in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(is_public_ip(ip), ip)

    def test_https_only(self):
        with self.assertRaisesRegex(FeedError, "https"):
            validate_feed_url("http://example.com/feed")

    def test_hostname_resolving_to_private_is_refused(self):
        res = lambda host, port, **k: [(socket.AF_INET, 0, 0, "", ("10.1.2.3", port))]
        with self.assertRaisesRegex(FeedError, "non-public"):
            validate_feed_url("https://innocent.example/feed", res)

    def test_one_private_among_many_addresses_is_refused(self):
        res = lambda host, port, **k: [(2, 0, 0, "", ("93.184.216.34", port)), (2, 0, 0, "", ("127.0.0.1", port))]
        with self.assertRaises(FeedError):
            validate_feed_url("https://rebind.example/feed", res)

    def test_public_ok(self):
        validate_feed_url("https://ok.example/feed", lambda h, p, **k: [(2, 0, 0, "", ("93.184.216.34", p))])


class Ingest(BaseCase):
    ingest_fixtures = False

    def test_dedupe_by_external_id_across_runs(self):
        a = ingest.ingest_all(self.conn, self.settings, self.clock, fixture_tag="x")
        b = ingest.ingest_all(self.conn, self.settings, self.clock, fixture_tag="x")
        self.assertGreater(a["new"], 0)
        self.assertEqual((b["new"], b["duplicate"]), (0, a["new"]))

    def test_dedupe_by_content_hash_and_url(self):
        ingest.ingest_all(self.conn, self.settings, self.clock, fixture_tag="x")
        n = self.conn.scalar("SELECT COUNT(*) FROM source_documents")
        d = self.conn.one("SELECT * FROM source_documents LIMIT 1")
        same_hash = self.conn.run("INSERT INTO source_documents (id,source_key,external_id,url,title,body,published_at,content_hash,fetched_at) "
                                  "VALUES ('z1',?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                                  (d["source_key"], "other-id", "https://other.test/u", d["title"], d["body"], d["published_at"], d["content_hash"], d["fetched_at"]))
        self.assertEqual(same_hash, 0)
        self.assertEqual(self.conn.scalar("SELECT COUNT(*) FROM source_documents"), n)

    def test_old_entries_dropped(self):
        far = FixedClock(NOW)
        ingest.ingest_all(self.conn, self.settings, far, fixture_tag="x")
        later = FixedClock(NOW + timedelta(days=45))
        st = ingest.ingest_all(self.conn, self.settings.with_(feed_freshness_days=30), later, fixture_tag="y")
        self.assertEqual(st["new"], 8)       # y-tagged docs are relative to the later clock, so they are fresh
        st2 = ingest.ingest_all(self.conn, self.settings.with_(feed_freshness_days=0), later, fixture_tag="z")
        self.assertEqual(st2["too_old"], 8)

    def test_one_failing_live_source_does_not_stop_others(self):
        self.conn.run("INSERT INTO sources (source_key,name,feed_url,language,mode) VALUES ('bad','Bad','https://bad.test/f','en','live')")
        self.conn.run("INSERT INTO sources (source_key,name,feed_url,language,mode) VALUES ('good','Good','https://good.test/f','en','live')")
        self.conn.commit()
        def fetcher(url):
            if "bad" in url:
                raise FeedError("boom")
            return RSS.replace("e.test/1", url.split("/")[2] + "/1").replace("World", url).encode()
        st = ingest.ingest_all(self.conn, self.settings.with_(feed_mode="live"), self.clock, fetcher=fetcher)
        self.assertIn("bad", st["errors"])
        self.assertEqual(self.conn.scalar("SELECT COUNT(*) FROM source_documents WHERE source_key='good'"), 1)

    def test_throttle_skips_recent_ingest(self):
        self.assertIsNotNone(ingest.ensure_fresh_ingest(self.conn, self.settings, self.clock))
        self.assertIsNone(ingest.ensure_fresh_ingest(self.conn, self.settings, self.clock))
        self.clock.advance(minutes=31)
        self.assertIsNotNone(ingest.ensure_fresh_ingest(self.conn, self.settings, self.clock))


class SourcesFile(unittest.TestCase):
    def test_shipped_file_is_valid_and_live_urls_are_https(self):
        import os
        from tests.helpers import ROOT
        src = ingest.load_sources_file(os.path.join(ROOT, "sources.yaml"))
        self.assertTrue(any(s["mode"] == "fixture" for s in src))
        for s in src:
            if s["mode"] == "live":
                self.assertTrue(s["url"].startswith("https://"), s)

    def test_http_live_source_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("sources:\n  - {key: a, name: A, url: 'http://x.test/f', mode: live}\n")
        with self.assertRaises(ValueError):
            ingest.load_sources_file(f.name)


if __name__ == "__main__":
    unittest.main()
