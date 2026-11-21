import email
import unittest
from email import policy

from app.clock import FixedClock
from app.config import ConfigError
from app.email_build import build_message, make_token, render_newsletter, unsubscribe_url, verify_token
from tests.helpers import NOW

DOC = {"headline": "Head <b>", "intro": "Intro", "items": [{"label": "S1", "title": "T <script>", "bullets": [{"text": "x & y", "citation": "S1"}]}]}
SRC = {"S1": {"title": "Src", "url": "https://e.test/a?x=1&y=2", "source_name": "E"}}


class Tokens(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(verify_token("k", make_token("k", "sub-1")), "sub-1")

    def test_tamper_and_wrong_key(self):
        t = make_token("k", "sub-1")
        self.assertIsNone(verify_token("other", t))
        self.assertIsNone(verify_token("k", t[:-2] + "AA"))
        self.assertIsNone(verify_token("k", make_token("k", "sub-2").split(".")[0] + "." + t.split(".")[1]))
        for junk in ("", "x", "a.b", "....", "%%%"):
            self.assertIsNone(verify_token("k", junk))

    def test_empty_secret_refused(self):
        with self.assertRaisesRegex(ConfigError, "SECRET_KEY"):
            make_token("", "x")


class Rendering(unittest.TestCase):
    def test_html_is_escaped(self):
        _, text, html = render_newsletter(DOC, SRC, "Al <x>", "en", "https://u.test/x")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("x &amp; y", html)
        self.assertIn("&amp;y=2", html)

    def test_german_labels(self):
        _, text, html = render_newsletter(DOC, SRC, "Ali", "de", "https://u.test/x")
        self.assertIn("Hallo Ali", text)
        self.assertIn("Abmelden", html)

    def test_unsubscribe_link_in_both_bodies(self):
        _, text, html = render_newsletter(DOC, SRC, "A", "en", "https://u.test/tok")
        self.assertIn("https://u.test/tok", text)
        self.assertIn("https://u.test/tok", html)


class Message(unittest.TestCase):
    def build(self, to="a@b.test", did="d1"):
        return build_message("News <n@brand.test>", to, "Subj ü", "text", "<p>html</p>", "https://u.test/tok", FixedClock(NOW), did)

    def test_single_recipient_and_headers(self):
        raw = self.build().as_bytes()
        m = email.message_from_bytes(raw, policy=policy.default)
        self.assertEqual(m["To"], "a@b.test")
        self.assertEqual(m["List-Unsubscribe"], "<https://u.test/tok>")
        self.assertEqual(m["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(m["Date"], "Wed, 04 Mar 2026 10:00:00 +0000")
        self.assertTrue(m["Message-ID"].endswith("@brand.test>"))
        self.assertEqual(m["Subject"], "Subj ü")

    def test_multipart_text_and_html(self):
        m = self.build()
        self.assertEqual([p.get_content_type() for p in m.iter_parts()], ["text/plain", "text/html"])

    def test_long_unsubscribe_url_not_mangled(self):
        long = "https://u.test/unsubscribe/" + "A" * 200
        m = email.message_from_bytes(build_message("n@b.test", "a@b.test", "s", "t", "h", long, FixedClock(NOW), "d").as_bytes(), policy=policy.default)
        self.assertEqual(m["List-Unsubscribe"], f"<{long}>")

    def test_message_ids_unique(self):
        self.assertNotEqual(self.build()["Message-ID"], self.build()["Message-ID"])

    def test_unsubscribe_url_uses_public_base(self):
        from app.config import Settings
        u = unsubscribe_url(Settings(secret_key="k", public_base_url="https://n.test/"), "sid")
        self.assertTrue(u.startswith("https://n.test/unsubscribe/"))


if __name__ == "__main__":
    unittest.main()
