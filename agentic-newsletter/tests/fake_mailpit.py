"""Minimal HTTP stand-in for the Mailpit API endpoints the smoke test uses, backed by FakeSMTP's captured messages.
NOTE: the endpoint shapes are written from knowledge of Mailpit's API, NOT checked against a real Mailpit here."""
from __future__ import annotations

import email
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMailpit:
    def __init__(self, smtp):
        self.smtp = smtp
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/v1/messages"):
                    out = []
                    for i, m in enumerate(outer.smtp.messages):
                        parsed = email.message_from_string(m["data"])
                        out.append({"ID": str(i), "Subject": parsed["Subject"],
                                    "To": [{"Name": "", "Address": a} for a in m["to"]]})
                    self._send(json.dumps({"total": len(out), "messages": out}).encode())
                elif self.path.startswith("/api/v1/message/") and self.path.endswith("/raw"):
                    idx = int(self.path.split("/")[4])
                    self._send(outer.smtp.messages[idx]["data"].encode("utf-8"), "message/rfc822")
                else:
                    self.send_response(404); self.end_headers()

            def do_DELETE(self):
                outer.smtp.messages.clear()
                self._send(b"ok", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
