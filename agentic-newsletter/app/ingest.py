"""Collector agent: load allowlisted sources, fetch, normalise, dedupe, store."""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path

import yaml

from . import feeds
from .clock import Clock, iso, parse_iso
from .config import Settings
from .db import Conn, new_id

log = logging.getLogger("collector")


def load_sources_file(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for s in data.get("sources", []):
        for field in ("key", "name", "url"):
            if not s.get(field):
                raise ValueError(f"sources file: entry {s!r} is missing '{field}'")
        if s.get("mode", "live") == "live" and not str(s["url"]).startswith("https://"):
            raise ValueError(f"sources file: live source {s['key']} must use https://")
        out.append(s)
    return out


def seed_sources(conn: Conn, path: str) -> int:
    n = 0
    for s in load_sources_file(path):
        n += conn.run(
            "INSERT INTO sources (source_key, name, feed_url, language, mode, quality, enabled) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT (source_key) DO NOTHING",
            (s["key"], s["name"], s["url"], s.get("language", "en"), s.get("mode", "live"), s.get("quality", "high")))
    return n


def content_hash(title: str, body: str) -> str:
    return hashlib.sha256((title.strip() + "\n" + body.strip()).encode("utf-8")).hexdigest()


def ingest_all(conn: Conn, settings: Settings, clock: Clock, fixture_tag: str | None = None,
               source_keys: list[str] | None = None, fetcher=None) -> dict:
    """Returns counts: {'sources': n, 'fetched': n, 'new': n, 'duplicate': n, 'too_old': n, 'errors': {key: reason}}."""
    now = clock.now()
    tag = fixture_tag or now.strftime("%Y%m%d")
    cutoff = now - timedelta(days=settings.feed_freshness_days)
    rows = conn.all("SELECT * FROM sources WHERE enabled = 1 AND mode = ?", (settings.feed_mode,))
    if source_keys:
        rows = [r for r in rows if r["source_key"] in source_keys]
    stats = {"sources": len(rows), "fetched": 0, "new": 0, "duplicate": 0, "too_old": 0, "errors": {}}
    for src in rows:
        key = src["source_key"]
        try:
            if settings.feed_mode == "fixture":
                entries = feeds.parse_feed(feeds.fixture_xml(key, now, tag))
            else:
                data = (fetcher or feeds.fetch_live)(src["feed_url"])
                entries = feeds.parse_feed(data)
        except Exception as exc:  # one broken feed must not stop the others
            stats["errors"][key] = f"{type(exc).__name__}: {exc}"
            log.warning("source %s failed: %s", key, exc)
            continue
        for e in entries:
            stats["fetched"] += 1
            if e.published_at < cutoff:
                stats["too_old"] += 1
                continue
            added = conn.run(
                "INSERT INTO source_documents (id, source_key, external_id, url, title, body, language, published_at, "
                "content_hash, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (new_id(), key, e.external_id[:500], e.url[:1000], e.title[:500], e.body[:20000], src["language"],
                 iso(e.published_at), content_hash(e.title, e.body), iso(now)))
            stats["new" if added else "duplicate"] += 1
    conn.run("INSERT INTO automation_state (state_key, value, updated_at) VALUES ('last_ingest', ?, ?) "
             "ON CONFLICT (state_key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
             (iso(now), iso(now)))
    conn.commit()
    log.info("ingest: %s", stats)
    return stats


def ensure_fresh_ingest(conn: Conn, settings: Settings, clock: Clock) -> dict | None:
    last = conn.scalar("SELECT value FROM automation_state WHERE state_key = 'last_ingest'")
    now = clock.now()
    if last and parse_iso(last) > now - timedelta(minutes=settings.ingest_min_interval_minutes):
        return None
    return ingest_all(conn, settings, clock)
