import socket
import unittest
from email.message import EmailMessage

from app.config import ConfigError, Settings
from app.mailer import MailError, Mailer, SmtpConfig, allowlist_matches, build_mailer
from tests.fake_smtp import FakeSMTP, make_cert

CERT, _ = make_cert()


def msg(to="rcpt@example.test"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "News <news@example.test>", to, "Hello"
    m.set_content("body")
    return m


class SmtpBase(unittest.TestCase):
    def server(self, **kw):
        s = FakeSMTP(**kw).start()
        self.addCleanup(s.stop)
        return s

    def mailer(self, srv, security="starttls", user="", pw="", mode="smtp", allow="rcpt@example.test", trusted=True, **kw):
        cfg = SmtpConfig("127.0.0.1", srv.port, security, user, pw, "News <news@example.test>", 5, CERT if trusted else None)
        return Mailer(cfg, mode, allow)


class Delivery(SmtpBase):
    def test_starttls_and_login_success(self):
        s = self.server(starttls=True, auth=("user", "secret"))
        self.mailer(s, "starttls", "user", "secret").send(msg(), "rcpt@example.test")
        self.assertEqual(len(s.messages), 1)
        self.assertTrue(s.messages[0]["tls"])
        self.assertEqual(s.messages[0]["to"], ["rcpt@example.test"])
        self.assertIn("news@example.test", s.messages[0]["from"])

    def test_implicit_tls_success(self):
        s = self.server(implicit_tls=True, auth=("user", "secret"))
        self.mailer(s, "ssl", "user", "secret").send(msg(), "rcpt@example.test")
        self.assertEqual(len(s.messages), 1)

    def test_plain_without_login_for_mailpit_mode(self):
        s = self.server()
        self.mailer(s, "none", mode="mailpit").send(msg("x@y.test"), "x@y.test")
        self.assertEqual(s.messages[0]["to"], ["x@y.test"])

    def test_login_mechanism_login_only(self):
        s = self.server(starttls=True, auth=("user", "secret"), auth_mechs="LOGIN")
        self.mailer(s, "starttls", "user", "secret").send(msg(), "rcpt@example.test")
        self.assertEqual(len(s.messages), 1)

    def test_non_ascii_password_works(self):
        s = self.server(starttls=True, auth=("user", "pässwörd-ü€"))
        self.mailer(s, "starttls", "user", "pässwörd-ü€").send(msg(), "rcpt@example.test")
        self.assertEqual(len(s.messages), 1)

    def test_recipient_refused_reports_send_stage_and_code(self):
        s = self.server(reject_rcpt={"rcpt@example.test"})
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "none").send(msg(), "rcpt@example.test")
        self.assertEqual((cm.exception.stage, cm.exception.code), ("send", 550))
        self.assertEqual(s.messages, [])


class LoginFailures(SmtpBase):
    def test_wrong_password_reports_server_code_and_text(self):
        s = self.server(starttls=True, auth=("user", "secret"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "user", "WRONG-PASSWORD").send(msg(), "rcpt@example.test")
        e = cm.exception
        self.assertEqual((e.stage, e.code), ("login", 535))
        self.assertIn("credentials invalid", str(e))
        self.assertNotIn("WRONG-PASSWORD", str(e))
        self.assertEqual(s.messages, [])

    def test_exactly_one_login_attempt(self):
        s = self.server(starttls=True, auth=("user", "secret"))
        with self.assertRaises(MailError):
            self.mailer(s, "starttls", "user", "bad").send(msg(), "rcpt@example.test")
        self.assertEqual(s.auth_attempts, 1)

    def test_hangup_after_rejection_is_reported_as_login_not_connection(self):
        s = self.server(starttls=True, auth=("user", "secret"), hangup_on_bad_auth=True)
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "user", "bad").send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "login")
        self.assertIn("535", str(cm.exception) + " 535")  # code may be absent when the server hangs up before replying
        self.assertEqual(s.auth_attempts, 1)

    def test_non_ascii_wrong_password_is_not_leaked(self):
        s = self.server(starttls=True, auth=("user", "good"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "user", "gehéim-ß").send(msg(), "rcpt@example.test")
        self.assertNotIn("gehéim", str(cm.exception))

    def test_credentials_never_sent_over_plaintext(self):
        s = self.server(auth=("user", "secret"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "none", "user", "secret").send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "login")
        self.assertIn("unencrypted", str(cm.exception))
        self.assertEqual(s.auth_attempts, 0)


class TlsSafety(SmtpBase):
    def test_untrusted_certificate_refused_before_credentials_are_sent(self):
        s = self.server(starttls=True, auth=("user", "secret"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "user", "secret", trusted=False).send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "STARTTLS")
        self.assertIn("not trusted", str(cm.exception))
        self.assertEqual(s.auth_attempts, 0)
        self.assertEqual(s.messages, [])

    def test_untrusted_certificate_implicit_tls(self):
        s = self.server(implicit_tls=True, auth=("user", "secret"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "ssl", "user", "secret", trusted=False).send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "TLS handshake")

    def test_no_silent_downgrade_when_starttls_missing(self):
        s = self.server(starttls=False, auth=("user", "secret"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "user", "secret").send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "STARTTLS")
        self.assertIn("refusing", str(cm.exception))
        self.assertEqual((s.auth_attempts, s.messages), (0, []))

    def test_wrong_security_mode_for_port_is_a_tls_error(self):
        s = self.server(starttls=True)           # plain-text greeting server
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "ssl").send(msg(), "rcpt@example.test")
        self.assertIn(cm.exception.stage, ("TLS handshake", "greeting", "connecting"))


class Connecting(SmtpBase):
    def test_connection_refused_names_the_connecting_stage(self):
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()   # a port nobody listens on
        m = Mailer(SmtpConfig("127.0.0.1", port, "none", timeout=3), "mailpit")
        with self.assertRaises(MailError) as cm:
            m.send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "connecting")
        self.assertIn("next step", str(cm.exception))

    def test_every_error_message_has_stage_and_hint(self):
        e = MailError("login", "bad", 535)
        self.assertTrue(str(e).startswith("[login] bad (server code 535)"))
        self.assertIn("SMTP_PASSWORD", str(e))


class Allowlist(SmtpBase):
    def test_matching_rules(self):
        self.assertTrue(allowlist_matches("me@x.test", "ME@x.test"))
        self.assertTrue(allowlist_matches("x.test", "anyone@x.test"))
        self.assertTrue(allowlist_matches("@x.test", "anyone@x.test"))
        self.assertTrue(allowlist_matches("a@b.test, me@x.test", "me@x.test"))
        self.assertFalse(allowlist_matches("me@x.test", "other@x.test"))
        self.assertFalse(allowlist_matches("x.test", "a@evilx.test"))
        self.assertFalse(allowlist_matches("", "me@x.test"))

    def test_empty_allowlist_refuses_before_any_connection(self):
        s = self.server(starttls=True, auth=("u", "p"))
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", "u", "p", allow="").send(msg(), "rcpt@example.test")
        self.assertEqual(cm.exception.stage, "allowlist")
        self.assertEqual((s.auth_attempts, s.messages), (0, []))

    def test_not_listed_recipient_refused_listed_sent(self):
        s = self.server()
        m = self.mailer(s, "none", allow="ok@example.test")
        with self.assertRaises(MailError):
            m.send(msg("no@example.test"), "no@example.test")
        m.send(msg("ok@example.test"), "ok@example.test")
        self.assertEqual([x["to"] for x in s.messages], [["ok@example.test"]])

    def test_demo_mode_ignores_allowlist(self):
        s = self.server()
        self.mailer(s, "none", mode="mailpit", allow="").send(msg("anyone@demo.test"), "anyone@demo.test")
        self.assertEqual(len(s.messages), 1)


class CheckConnection(SmtpBase):
    def test_connect_and_tls_only_no_login_no_send(self):
        s = self.server(starttls=True, auth=("user", "secret"))
        info = self.mailer(s, "starttls", "user", "secret").check_connection()
        self.assertTrue(info["connected"])
        self.assertEqual(info["tls"], "STARTTLS verified")
        self.assertIn("PLAIN", info["auth_mechanisms"])
        self.assertEqual((s.auth_attempts, s.messages), (0, []))

    def test_check_reports_untrusted_certificate(self):
        s = self.server(starttls=True)
        with self.assertRaises(MailError) as cm:
            self.mailer(s, "starttls", trusted=False).check_connection()
        self.assertIn("not trusted", str(cm.exception))


class BuildMailer(unittest.TestCase):
    def test_demo_mode_defaults(self):
        mailer, sender = build_mailer(Settings())
        self.assertEqual((mailer.mode, mailer.cfg.host, mailer.cfg.port, mailer.cfg.security), ("mailpit", "mailpit", 1025, "none"))

    def test_smtp_mode_needs_host_and_sender(self):
        with self.assertRaisesRegex(ConfigError, "SMTP_HOST, SMTP_SENDER"):
            build_mailer(Settings(email_mode="smtp"))

    def test_sender_is_separate_from_login(self):
        m, sender = build_mailer(Settings(email_mode="smtp", smtp_host="h", smtp_sender="news@brand.test", smtp_username="login@provider.test"))
        self.assertEqual((sender, m.cfg.username), ("news@brand.test", "login@provider.test"))


if __name__ == "__main__":
    unittest.main()
