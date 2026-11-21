"""Feed fixtures, RSS/Atom parsing, SSRF-safe live fetching."""
from __future__ import annotations

import html
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser

UTC = timezone.utc
MAX_FEED_BYTES = 2_000_000


class FeedError(Exception):
    pass


@dataclass
class Entry:
    external_id: str
    url: str
    title: str
    body: str
    published_at: datetime


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(text: str) -> str:
    p = _Text()
    p.feed(text or "")
    p.close()
    return " ".join(html.unescape("".join(p.parts)).split())


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str | bytes) -> list[Entry]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"feed is not valid XML: {exc}") from exc
    entries: list[Entry] = []
    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        f = {}
        link = ""
        for child in node:
            cname = _local(child.tag)
            if cname == "link":
                link = (child.attrib.get("href") or child.text or link or "").strip()
            elif cname not in f:
                f[cname] = "".join(child.itertext()).strip()   # includes text inside inline markup
        title = strip_html(f.get("title", ""))
        body = strip_html(f.get("description") or f.get("summary") or f.get("content") or "")
        published = _parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date"))
        ext = f.get("guid") or f.get("id") or link
        if not (title and link and published and ext):
            continue  # unusable entry
        entries.append(Entry(ext, link, title, body or title, published))
    return entries


# ---------------------------------------------------------------- fixtures
_FIXTURES = {
    "fixture-tech-en": [
        ("ai", "Researchers unveil efficient neural network for edge devices",
         "A team of researchers presented a new artificial intelligence model that runs on small devices. "
         "The neural network uses far less memory than earlier designs. Developers say machine learning workloads can now run locally."),
        ("ai", "Generative AI tools reach more classrooms",
         "Schools are adopting generative AI tools for lesson planning. Teachers report that artificial intelligence saves preparation time. "
         "Districts plan training sessions next term."),
        ("robotics", "Warehouse robots learn to grasp irregular parcels",
         "Engineers demonstrated robots that can grasp irregular parcels. The robotics team trained the grippers with 2,000 practice attempts. "
         "Pilot deployments are planned for next year."),
        ("robotics", "Humanoid robot completes factory shift trial",
         "A humanoid robot completed an eight hour trial shift at a car factory. The robotics company said the machine moved parts between stations. "
         "Safety engineers monitored the trial throughout."),
        ("devops", "Kubernetes release simplifies cluster upgrades",
         "The new Kubernetes release makes cluster upgrades simpler. Maintainers say the deployment process needs fewer manual steps. "
         "Docker users get improved container build caching."),
        ("business", "Startup raises funds to expand cloud storage",
         "A cloud storage startup announced new investment. The company reported revenue growth over the past year. Executives plan to hire engineers."),
    ],
    "fixture-news-de": [
        ("ai-de", "Neue KI hilft bei der Auswertung von Satellitenbildern",
         "Forscher stellen eine neue künstliche Intelligenz für Satellitenbilder vor. Die KI erkennt Veränderungen in Wäldern schneller als bisherige Verfahren. "
         "Das Projekt wird von mehreren Universitäten getragen."),
        ("robotics-de", "Roboter unterstützen Pflegekräfte im Test",
         "In einem Pilotprojekt unterstützen Roboter Pflegekräfte bei einfachen Aufgaben. Die Robotik Experten werten die Ergebnisse im Herbst aus."),
    ],
}


def fixture_xml(source_key: str, now: datetime, tag: str) -> str:
    """RSS for a fixture source. Dates are relative to `now`; `tag` makes ids/urls/content unique per edition."""
    items = _FIXTURES.get(source_key)
    if items is None:
        raise FeedError(f"unknown fixture feed {source_key!r}")
    out = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>',
           f"<title>{html.escape(source_key)}</title>"]
    for i, (kind, title, body) in enumerate(items, start=1):
        when = now - timedelta(hours=2 * i)
        body_t = f"{body} (Fixture edition {tag}.)"
        out.append("<item><title>%s</title><link>https://fixtures.example.test/%s/%s/%d</link>"
                   "<guid isPermaLink=\"false\">%s-%s-%d</guid><pubDate>%s</pubDate><description>%s</description></item>"
                   % (html.escape(title), source_key, tag, i, source_key, tag, i,
                      format_datetime(when), html.escape(body_t)))
    out.append("</channel></rss>")
    return "".join(out)


# ---------------------------------------------------------------- SSRF-safe live fetch
def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def validate_feed_url(url: str, resolver=socket.getaddrinfo) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise FeedError(f"feed URL must use https:// (got {parts.scheme or 'none'}://)")
    if not parts.hostname:
        raise FeedError("feed URL has no host")
    try:
        infos = resolver(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FeedError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        ip = info[4][0]
        if not is_public_ip(ip):
            raise FeedError(f"refusing {parts.hostname}: resolves to non-public address {ip}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # we follow redirects manually so each hop is validated
        return None


def fetch_live(url: str, timeout: int = 15, resolver=socket.getaddrinfo, max_redirects: int = 3) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_redirects + 1):
        validate_feed_url(current, resolver)
        req = urllib.request.Request(current, headers={"User-Agent": "agentic-newsletter/1.0 (+rss reader)"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read(MAX_FEED_BYTES + 1)
                if len(data) > MAX_FEED_BYTES:
                    raise FeedError(f"feed larger than {MAX_FEED_BYTES} bytes")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            raise FeedError(f"HTTP {exc.code} from {urllib.parse.urlsplit(current).hostname}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeedError(f"cannot fetch {urllib.parse.urlsplit(current).hostname}: {exc}") from exc
    raise FeedError("too many redirects")
