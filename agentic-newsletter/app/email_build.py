"""Rendering + message construction + signed unsubscribe tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from html import escape

from .config import ConfigError

_T = {
    "en": {"hello": "Hi {name},", "sources": "Sources", "unsub": "Unsubscribe", "footer": "You receive this because you subscribed to topic briefings."},
    "de": {"hello": "Hallo {name},", "sources": "Quellen", "unsub": "Abmelden", "footer": "Du erhältst diese E-Mail, weil du Themen-Briefings abonniert hast."},
}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(secret: str, subscriber_id: str) -> str:
    if not secret:
        raise ConfigError("SECRET_KEY is empty - run ./run.sh init-env (or set SECRET_KEY) before sending emails")
    mac = hmac.new(secret.encode(), subscriber_id.encode(), hashlib.sha256).digest()[:16]
    return f"{_b64(subscriber_id.encode())}.{_b64(mac)}"


def verify_token(secret: str, token: str) -> str | None:
    try:
        sid_b, mac_b = token.split(".", 1)
        sid = _unb64(sid_b).decode()
        good = hmac.new(secret.encode(), sid.encode(), hashlib.sha256).digest()[:16]
        return sid if secret and hmac.compare_digest(good, _unb64(mac_b)) else None
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        return None


def unsubscribe_url(settings, subscriber_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/unsubscribe/{make_token(settings.secret_key, subscriber_id)}"


def render_newsletter(doc: dict, sources: dict[str, dict], name: str, language: str, unsub_url: str) -> tuple[str, str, str]:
    """doc: validated newsletter dict (only verified bullets). sources: label -> {title,url,source_name}."""
    t = _T.get(language, _T["en"])
    subject = doc["headline"]
    text = [t["hello"].format(name=name), "", doc["intro"], ""]
    html = [f'<html lang="{escape(language)}"><body style="font-family:Arial,sans-serif;max-width:640px;margin:auto">',
            f"<h1>{escape(doc['headline'])}</h1>", f"<p>{escape(t['hello'].format(name=name))}</p>", f"<p>{escape(doc['intro'])}</p>"]
    order: list[str] = []
    for item in doc["items"]:
        label = item["label"]
        src = sources.get(label, {})
        text.append(item["title"])
        html.append(f"<h2>{escape(item['title'])}</h2><ul>")
        for b in item["bullets"]:
            n = order.index(b["citation"]) + 1 if b["citation"] in order else (order.append(b["citation"]) or len(order))
            text.append(f"  - {b['text']} [{n}]")
            html.append(f"<li>{escape(b['text'])} <sup>[{n}]</sup></li>")
        html.append("</ul>")
        text.append("")
    text.append(t["sources"] + ":")
    html.append(f"<h3>{escape(t['sources'])}</h3><ol>")
    for lab in order:
        s = sources.get(lab, {})
        text.append(f"  [{order.index(lab) + 1}] {s.get('title', lab)} - {s.get('url', '')}")
        html.append(f'<li><a href="{escape(s.get("url", ""), quote=True)}">{escape(s.get("title", lab))}</a> '
                    f"({escape(s.get('source_name', ''))})</li>")
    html.append("</ol>")
    text += ["", t["footer"], f"{t['unsub']}: {unsub_url}"]
    html.append(f'<hr><p style="color:#666;font-size:12px">{escape(t["footer"])} '
                f'<a href="{escape(unsub_url, quote=True)}">{escape(t["unsub"])}</a></p></body></html>')
    return subject, "\n".join(text), "".join(html)


def build_message(from_addr: str, to_addr: str, subject: str, text: str, html: str, unsub_url: str,
                  clock, draft_id: str) -> EmailMessage:
    # long unbroken header values (unsubscribe URL) must not be folded/RFC2047-encoded: raise the fold width
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=2000))
    msg["From"] = from_addr
    msg["To"] = to_addr  # exactly one recipient per message
    msg["Subject"] = subject
    msg["Date"] = format_datetime(clock.now())
    addr = parseaddr(from_addr)[1]
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else "agentic-nl.local"
    msg["Message-ID"] = make_msgid(idstring=draft_id[:12], domain=domain)
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg
