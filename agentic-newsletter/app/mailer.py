"""SMTP sender with stage-specific errors, strict TLS, single login attempt, send allowlist."""
from __future__ import annotations

import base64
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr

_HINTS = {
    "allowlist": "add the recipient (or its domain) to REAL_SEND_ALLOWLIST if you really want to send to it",
    "connecting": "check host/port, that the server is running, and your firewall",
    "greeting": "the server answered but did not give a normal SMTP greeting - wrong port or not an SMTP server?",
    "TLS handshake": "check SMTP_SECURITY=ssl matches the port (usually 465)",
    "EHLO": "the server rejected EHLO",
    "STARTTLS": "check SMTP_SECURITY=starttls matches the port (usually 587)",
    "login": "check SMTP_USERNAME / SMTP_PASSWORD (many providers need an app password)",
    "send": "the server refused the sender, recipient or message content",
}


class MailError(Exception):
    def __init__(self, stage: str, message: str, code: int | None = None):
        self.stage, self.message, self.code = stage, message, code
        super().__init__(f"[{stage}] {message}" + (f" (server code {code})" if code else "") +
                         f" - next step: {_HINTS.get(stage, 'see logs')}")


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str = "none"          # starttls | ssl | none
    username: str = ""
    password: str = ""
    sender: str = ""
    timeout: int = 20
    ca_file: str | None = None      # extra CA for tests / private relays; default system store


def allowlist_matches(allowlist: str, recipient: str) -> bool:
    rcpt = recipient.strip().lower()
    for entry in (e.strip().lower() for e in allowlist.split(",")):
        if not entry:
            continue
        if "@" in entry and not entry.startswith("@"):
            if rcpt == entry:
                return True
        else:
            if rcpt.endswith("@" + entry.lstrip("@")):
                return True
    return False


class Mailer:
    def __init__(self, cfg: SmtpConfig, mode: str = "mailpit", allowlist: str = ""):
        self.cfg, self.mode, self.allowlist = cfg, mode, allowlist

    # ------------------------------------------------------------ helpers
    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()  # verifies certificates and hostnames
        if self.cfg.ca_file:
            ctx.load_verify_locations(self.cfg.ca_file)
        return ctx

    def _open(self) -> smtplib.SMTP:
        c = self.cfg
        try:
            if c.security == "ssl":
                try:
                    cli = smtplib.SMTP_SSL(c.host, c.port, timeout=c.timeout, context=self._context(), local_hostname="localhost")
                except ssl.SSLCertVerificationError as exc:
                    raise MailError("TLS handshake", f"server certificate not trusted: {exc.verify_message}") from exc
                except ssl.SSLError as exc:
                    raise MailError("TLS handshake", f"TLS failed: {exc.reason or exc}") from exc
            else:
                cli = smtplib.SMTP(c.host, c.port, timeout=c.timeout, local_hostname="localhost")
            return cli
        except smtplib.SMTPConnectError as exc:
            raise MailError("greeting", exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error), exc.smtp_code) from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise MailError("greeting", f"server closed the connection before greeting ({exc})") from exc
        except (OSError, socket.timeout) as exc:
            raise MailError("connecting", f"cannot connect to {c.host}:{c.port}: {exc}") from exc

    def _session(self) -> smtplib.SMTP:
        c = self.cfg
        cli = self._open()
        try:
            try:
                cli.ehlo()
            except smtplib.SMTPException as exc:
                raise MailError("EHLO", str(exc)) from exc
            if c.security == "starttls":
                if not cli.has_extn("starttls"):
                    raise MailError("STARTTLS", "server does not offer STARTTLS - refusing to continue without encryption")
                try:
                    code, reply = cli.starttls(context=self._context())
                except ssl.SSLCertVerificationError as exc:
                    raise MailError("STARTTLS", f"server certificate not trusted: {exc.verify_message}") from exc
                except ssl.SSLError as exc:
                    raise MailError("STARTTLS", f"TLS failed: {exc.reason or exc}") from exc
                except smtplib.SMTPResponseException as exc:
                    raise MailError("STARTTLS", exc.smtp_error.decode(errors="replace"), exc.smtp_code) from exc
                except smtplib.SMTPServerDisconnected as exc:
                    raise MailError("STARTTLS", "server closed the connection during STARTTLS") from exc
                if code != 220:
                    raise MailError("STARTTLS", reply.decode(errors="replace"), code)
                cli.ehlo()
            if c.username:
                self._login(cli)
            return cli
        except BaseException:
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    def _login(self, cli: smtplib.SMTP) -> None:
        c = self.cfg
        if c.security == "none":
            raise MailError("login", "refusing to send credentials over an unencrypted connection (SMTP_SECURITY=none)")
        mechs = (cli.esmtp_features.get("auth", "") or "").upper().split()
        try:
            if "PLAIN" in mechs or not mechs:
                token = base64.b64encode(f"\0{c.username}\0{c.password}".encode("utf-8")).decode()
                code, reply = cli.docmd("AUTH", "PLAIN " + token)   # exactly one attempt
            elif "LOGIN" in mechs:
                code, reply = cli.docmd("AUTH", "LOGIN")
                if code == 334:
                    code, reply = cli.docmd(base64.b64encode(c.username.encode()).decode())
                if code == 334:
                    code, reply = cli.docmd(base64.b64encode(c.password.encode("utf-8")).decode())
            else:
                raise MailError("login", f"server offers no PLAIN/LOGIN authentication (offers: {' '.join(mechs)})")
        except smtplib.SMTPServerDisconnected as exc:
            raise MailError("login", "server closed the connection while logging in (no reply) - usually rejected credentials") from exc
        if code != 235:
            raise MailError("login", reply.decode(errors="replace").strip(), code)

    # ------------------------------------------------------------ API
    def check_connection(self) -> dict:
        """Connect + TLS only. No login, no send."""
        c = self.cfg
        try:
            cli = self._open()
            try:
                cli.ehlo()
                info = {"connected": True, "starttls_offered": cli.has_extn("starttls"),
                        "auth_mechanisms": (cli.esmtp_features.get("auth", "") or "").split()}
                if c.security == "starttls":
                    if not info["starttls_offered"]:
                        raise MailError("STARTTLS", "server does not offer STARTTLS")
                    cli.starttls(context=self._context())
                    cli.ehlo()   # capabilities (AUTH) are only advertised after TLS is up
                    info["auth_mechanisms"] = (cli.esmtp_features.get("auth", "") or "").split()
                    info["tls"] = "STARTTLS verified"
                elif c.security == "ssl":
                    info["tls"] = "implicit TLS verified"
                return info
            finally:
                cli.close()
        except ssl.SSLCertVerificationError as exc:
            raise MailError("STARTTLS", f"server certificate not trusted: {exc.verify_message}") from exc

    def send(self, msg: EmailMessage, recipient: str) -> None:
        if self.mode == "smtp" and not allowlist_matches(self.allowlist, recipient):
            raise MailError("allowlist", f"recipient {recipient} is not in REAL_SEND_ALLOWLIST; refusing to send real email")
        sender = parseaddr(self.cfg.sender)[1] or self.cfg.sender
        cli = self._session()
        try:
            try:
                cli.send_message(msg, from_addr=sender, to_addrs=[recipient])
            except smtplib.SMTPRecipientsRefused as exc:
                code, text = next(iter(exc.recipients.values()))
                raise MailError("send", f"recipient refused: {text.decode(errors='replace')}", code) from exc
            except smtplib.SMTPSenderRefused as exc:
                raise MailError("send", f"sender refused: {exc.smtp_error.decode(errors='replace')}", exc.smtp_code) from exc
            except smtplib.SMTPResponseException as exc:
                err = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
                raise MailError("send", err, exc.smtp_code) from exc
            except smtplib.SMTPServerDisconnected as exc:
                raise MailError("send", "server closed the connection while sending") from exc
            except (OSError, smtplib.SMTPException) as exc:
                raise MailError("send", f"{type(exc).__name__}: {exc}") from exc
            try:
                cli.quit()
            except Exception:  # noqa: BLE001 - message already accepted
                pass
        finally:
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass


def build_mailer(settings) -> tuple[Mailer, str]:
    """Returns (mailer, From header)."""
    if settings.email_mode == "mailpit":
        cfg = SmtpConfig(settings.mailpit_smtp_host, settings.mailpit_smtp_port, "none", sender=settings.mail_from_demo,
                         timeout=settings.smtp_timeout_seconds)
        return Mailer(cfg, "mailpit"), settings.mail_from_demo
    missing = [n for n, v in (("SMTP_HOST", settings.smtp_host), ("SMTP_SENDER", settings.smtp_sender)) if not v]
    if missing:
        from .config import ConfigError
        raise ConfigError(f"EMAIL_MODE=smtp needs {', '.join(missing)} to be set")
    cfg = SmtpConfig(settings.smtp_host, settings.smtp_port, settings.smtp_security, settings.smtp_username,
                     settings.smtp_password, settings.smtp_sender, settings.smtp_timeout_seconds)
    return Mailer(cfg, "smtp", settings.real_send_allowlist), settings.smtp_sender
