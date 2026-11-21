"""A small real SMTP server (sockets + ssl) for tests: STARTTLS, implicit TLS, AUTH PLAIN/LOGIN, configurable failures."""
from __future__ import annotations

import base64
import datetime
import ipaddress
import socket
import ssl
import tempfile
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_CERT_CACHE: dict = {}


def make_cert() -> tuple[str, str]:
    """Self-signed cert for localhost / 127.0.0.1. Returns (cert_path, key_path). Cached per process."""
    if _CERT_CACHE:
        return _CERT_CACHE["paths"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    d = Path(tempfile.mkdtemp(prefix="smtp-test-cert-"))
    cp, kp = d / "cert.pem", d / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    _CERT_CACHE["paths"] = (str(cp), str(kp))
    return _CERT_CACHE["paths"]


class FakeSMTP:
    def __init__(self, *, starttls=False, implicit_tls=False, auth: tuple[str, str] | None = None,
                 hangup_on_bad_auth=False, reject_rcpt: set[str] | None = None, offer_auth=True,
                 auth_mechs="PLAIN LOGIN"):
        self.starttls, self.implicit_tls, self.auth = starttls, implicit_tls, auth
        self.hangup_on_bad_auth, self.reject_rcpt, self.offer_auth = hangup_on_bad_auth, set(reject_rcpt or ()), offer_auth
        self.auth_mechs = auth_mechs
        self.messages: list[dict] = []      # {"from","to":[...],"data": str}
        self.auth_attempts = 0
        self.cert, self.key = make_cert()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _ctx(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        return ctx

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            self._session(conn)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _session(self, conn):
        self._live = conn
        try:
            self._session_inner(conn)
        finally:
            try:
                self._live.close()
            except OSError:
                pass

    def _session_inner(self, conn):
        conn.settimeout(10)
        tls = False
        if self.implicit_tls:
            conn = self._ctx().wrap_socket(conn, server_side=True)
            self._live = conn
            tls = True
        rf = conn.makefile("rb")

        def send(line: str):
            conn.sendall((line + "\r\n").encode())

        def readline() -> str | None:
            raw = rf.readline()
            return None if not raw else raw.decode("utf-8", "replace").rstrip("\r\n")

        send("220 fake.local ESMTP ready")
        mail_from, rcpts = "", []
        while True:
            line = readline()
            if line is None:
                return
            up = line.upper()
            if up.startswith("EHLO") or up.startswith("HELO"):
                lines = ["250-fake.local"]
                if self.starttls and not tls:
                    lines.append("250-STARTTLS")
                if self.auth and self.offer_auth and (tls or not self.starttls):
                    lines.append("250-AUTH " + self.auth_mechs)
                lines.append("250 8BITMIME")
                send("\r\n".join(lines))
            elif up == "STARTTLS" and self.starttls and not tls:
                send("220 Ready to start TLS")
                conn = self._ctx().wrap_socket(conn, server_side=True)
                self._live = conn
                rf, tls = conn.makefile("rb"), True
            elif up.startswith("AUTH PLAIN"):
                self.auth_attempts += 1
                parts = base64.b64decode(line.split(" ", 2)[2]).decode("utf-8").split("\0")
                self._auth_verdict(send, conn, parts[1], parts[2])
                if self.hangup_on_bad_auth and (parts[1], parts[2]) != self.auth:
                    return
            elif up == "AUTH LOGIN":
                self.auth_attempts += 1
                send("334 VXNlcm5hbWU6")
                user = base64.b64decode(readline() or "").decode()
                send("334 UGFzc3dvcmQ6")
                pw = base64.b64decode(readline() or "").decode("utf-8")
                self._auth_verdict(send, conn, user, pw)
                if self.hangup_on_bad_auth and (user, pw) != self.auth:
                    return
            elif up.startswith("MAIL FROM"):
                mail_from, rcpts = line[10:].strip(), []
                send("250 OK")
            elif up.startswith("RCPT TO"):
                addr = line[8:].strip().strip("<>").split(">")[0].strip("<")
                if addr.lower() in self.reject_rcpt:
                    send("550 5.1.1 mailbox unavailable")
                else:
                    rcpts.append(addr)
                    send("250 OK")
            elif up == "DATA":
                send("354 End data with <CR><LF>.<CR><LF>")
                data = []
                while True:
                    l = readline()
                    if l is None:
                        return
                    if l == ".":
                        break
                    data.append(l[1:] if l.startswith("..") else l)
                self.messages.append({"from": mail_from, "to": rcpts, "data": "\n".join(data), "tls": tls})
                send("250 2.0.0 queued")
            elif up == "RSET" or up == "NOOP":
                send("250 OK")
            elif up == "QUIT":
                send("221 bye")
                return
            else:
                send("502 command not implemented")

    def _auth_verdict(self, send, conn, user, pw):
        if self.auth and (user, pw) == self.auth:
            send("235 2.7.0 Authentication successful")
        else:
            send("535 5.7.8 Authentication credentials invalid")
