"""Agent pipeline as an explicit state machine:
curator -> summariser -> verifier -> risk -> decision -> personaliser
Each node has typed state in/out, bounded retries and an agent_runs audit record."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from . import topics as T
from .clock import iso, parse_iso
from .db import Conn, new_id
from .email_build import render_newsletter, unsubscribe_url
from .llm import validate_newsletter


class PipelineError(Exception):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")


@dataclass
class DraftState:
    newsletter_id: str
    draft_id: str
    subscriber: dict
    prefs: dict
    schedule_topics: list[str]
    schedule_source_keys: list[str]
    selected: list[dict] = field(default_factory=list)   # docs with label, score, reason, topic
    relevance: float = 0.0
    topic_labels: list[str] = field(default_factory=list)
    newsletter: dict | None = None
    claims: list[dict] = field(default_factory=list)
    removed: int = 0
    risk: str = "low"
    risk_reasons: list[str] = field(default_factory=list)
    decision: str = ""
    reason: str = ""
    subject: str = ""
    text: str = ""
    html: str = ""
    audit: list[dict] = field(default_factory=list)
    gen_failure: dict | None = None   # written AFTER the savepoint rolls back, so it is never lost


def decide(risk: str, relevance: float, n_articles: int, relevance_min: float = 0.6, relevance_high: float = 0.8) -> tuple[str, str]:
    """The decision matrix. Returns (decision, reason). decision in auto_send | review | skip."""
    if relevance < relevance_min or n_articles < 1:
        return "skip", "low_relevance"
    if risk in ("high", "critical"):
        return "review", f"risk={risk}"
    if risk == "low":
        if relevance >= relevance_high and n_articles >= 2:
            return "auto_send", "low risk, high relevance"
        if relevance >= relevance_min and n_articles >= 1:
            return "auto_send", "low risk, acceptable relevance"
    if risk == "medium":
        return "review", "risk=medium"
    return "review", f"unrecognised risk {risk!r}"


def assess_risk(selected: list[dict], removed: int, total_claims: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = 0
    text = " ".join(d["title"] + " " + d["body"] for d in selected)
    if T.hits(text, T.CRITICAL):
        level, reasons = 3, reasons + ["critical-sensitivity keywords present"]
    elif T.hits(text, T.SENSITIVE):
        level = max(level, 1)
        reasons.append("sensitive topic keywords: " + ", ".join(sorted(T.hits(text, T.SENSITIVE))))
    if any(d.get("quality") == "low" for d in selected):
        level = max(level, 1)
        reasons.append("low-quality source")
    if removed and total_claims:
        frac = removed / total_claims
        level = max(level, 2 if frac >= 0.5 else 1)
        reasons.append(f"{removed}/{total_claims} claims failed verification")
    return ["low", "medium", "high", "critical"][level], reasons


# ---------------------------------------------------------------- nodes
def _audit(state: DraftState, ctx, node: str, fn: Callable[[], str], retries: int, inp: str, model: str | None, pv: str | None):
    attempts, t0 = 0, time.monotonic()
    while True:
        attempts += 1
        try:
            out = fn()
            state.audit.append(dict(node=node, model=model, prompt_version=pv, input_summary=inp, output_summary=out,
                                    status="ok", error=None, attempts=attempts, duration_ms=int((time.monotonic() - t0) * 1000)))
            return
        except Exception as exc:  # noqa: BLE001
            if attempts > retries:
                state.audit.append(dict(node=node, model=model, prompt_version=pv, input_summary=inp, output_summary=None,
                                        status="failed", error=str(exc)[:500], attempts=attempts,
                                        duration_ms=int((time.monotonic() - t0) * 1000)))
                if isinstance(exc, PipelineError):
                    raise
                raise PipelineError(node, str(exc)) from exc


def flush_audit(conn: Conn, ctx, state: DraftState) -> None:
    g = state.gen_failure
    if g:
        conn.run("INSERT INTO generation_runs (id, draft_id, provider, model, mode, prompt_version, input_document_count, "
                 "status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (new_id(), state.draft_id, g["provider"], g["model"], g["mode"], g["pv"], g["n"], "failed", g["error"],
                  iso(ctx.clock.now())))
        state.gen_failure = None
    for a in state.audit:
        conn.run("INSERT INTO agent_runs (id, newsletter_id, draft_id, node, model, prompt_version, input_summary, output_summary, "
                 "status, error, attempts, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (new_id(), state.newsletter_id, state.draft_id, a["node"], a["model"], a["prompt_version"],
                  a["input_summary"], a["output_summary"], a["status"], a["error"], a["attempts"], a["duration_ms"],
                  iso(ctx.clock.now())))
    state.audit.clear()


def node_curator(state: DraftState, conn: Conn, ctx) -> str | None:
    s = ctx.settings
    def work() -> str:
        topics = T.load_topics(conn)
        sub_topics = [r["topic_key"] for r in conn.all("SELECT topic_key FROM subscriber_topics WHERE subscriber_id = ?",
                                                       (state.subscriber["id"],))]
        use = [t for t in sub_topics if t in topics and (not state.schedule_topics or t in state.schedule_topics)]
        state.topic_labels = [topics[t]["label"] for t in use]
        if not use:
            state.decision, state.reason = "skip", "no_new_articles"
            return "no matching topics for this subscriber"
        cutoff = ctx.clock.now().timestamp() - state.prefs["lookback_hours"] * 3600
        from datetime import datetime, timezone
        cutoff_iso = iso(datetime.fromtimestamp(cutoff, timezone.utc))
        rows = conn.all(
            "SELECT d.*, s.name AS source_name, s.quality AS quality FROM source_documents d "
            "JOIN sources s ON s.source_key = d.source_key WHERE d.published_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM article_deliveries a WHERE a.subscriber_id = ? AND a.document_id = d.id) "
            "AND NOT EXISTS (SELECT 1 FROM newsletter_source_documents n JOIN draft_emails e ON e.id = n.draft_id "
            "WHERE e.subscriber_id = ? AND n.document_id = d.id AND e.status IN ('draft','pending_review')) "
            "ORDER BY d.published_at DESC, d.id", (cutoff_iso, state.subscriber["id"], state.subscriber["id"]))
        allowed_sources = set(state.schedule_source_keys)
        pref_sources = set(json.loads(state.prefs.get("source_keys_json") or "[]"))
        excluded = json.loads(state.prefs.get("excluded_keywords_json") or "[]")
        scored = []
        for d in rows:
            if allowed_sources and d["source_key"] not in allowed_sources:
                continue
            if pref_sources and d["source_key"] not in pref_sources:
                continue
            if excluded and T.hits(d["title"] + " " + d["body"], excluded):
                continue
            best = (0.0, "", "")
            for t in use:
                sc, why = T.score_text(d["title"], d["body"], topics[t]["keywords"])
                if sc > best[0]:
                    best = (sc, why, t)
            if best[0] >= s.curator_min_score:
                d.update(score=best[0], reason=best[1], topic=best[2])
                scored.append(d)
        scored = sorted(scored, key=lambda d: (-d["score"], -parse_iso(d["published_at"]).timestamp()))
        state.selected = scored[: state.prefs["max_articles"]]
        for i, d in enumerate(state.selected, start=1):
            d["label"] = f"S{i}"
        if not state.selected:
            state.decision, state.reason = "skip", "no_new_articles"
            return "no new relevant articles"
        state.relevance = round(sum(d["score"] for d in state.selected) / len(state.selected), 3)
        for d in state.selected:
            conn.run("INSERT INTO newsletter_source_documents (newsletter_id, draft_id, document_id, relevance_score, "
                     "relevance_reason, selected_at) VALUES (?,?,?,?,?,?)",
                     (state.newsletter_id, state.draft_id, d["id"], d["score"], d["reason"], iso(ctx.clock.now())))
        if state.relevance < s.relevance_min:   # cheap pre-check: skip BEFORE any LLM generation cost
            state.decision, state.reason = "skip", "low_relevance"
        return f"selected {len(state.selected)} articles, mean relevance {state.relevance}"
    _audit(state, ctx, "curator", work, 0, f"subscriber topics, lookback {state.prefs['lookback_hours']}h", None, None)
    return "end" if state.decision == "skip" else None


def node_summariser(state: DraftState, conn: Conn, ctx) -> str | None:
    llm = ctx.llm
    articles = [{"label": d["label"], "title": d["title"], "body": d["body"], "source_name": d["source_name"], "url": d["url"]}
                for d in state.selected]
    labels = {a["label"] for a in articles}

    def work() -> str:
        raw = llm.summarise(articles, state.subscriber["language"], state.prefs["summary_style"], state.topic_labels)
        try:
            doc = validate_newsletter(json.loads(raw), labels)
        except (ValueError, TypeError) as first_err:
            try:   # exactly one repair attempt
                raw2 = llm.repair(raw, str(first_err))
                doc = validate_newsletter(json.loads(raw2), labels)
            except Exception as second_err:  # noqa: BLE001
                state.gen_failure = {"provider": llm.name, "model": llm.model, "mode": llm.mode, "pv": llm.prompt_version,
                                     "n": len(articles), "error": f"invalid JSON after repair: {second_err}"[:500]}
                raise PipelineError("summariser", f"model output invalid and repair failed: {first_err}") from second_err
        for item, art in zip(doc["items"], articles):   # keep labels aligned with the source order
            item["label"] = item.get("label") or art["label"]
            if item["label"] not in labels:
                item["label"] = item["bullets"][0]["citation"]
        state.newsletter = doc
        conn.run("INSERT INTO generation_runs (id, draft_id, provider, model, mode, prompt_version, input_document_count, "
                 "status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (new_id(), state.draft_id, llm.name, llm.model, llm.mode, llm.prompt_version, len(articles), "ok", None,
                  iso(ctx.clock.now())))
        return f"{len(doc['items'])} items"
    _audit(state, ctx, "summariser", work, 1, f"{len(articles)} articles", llm.model, llm.prompt_version)
    return None


def node_verifier(state: DraftState, conn: Conn, ctx) -> str | None:
    by_label = {d["label"]: d for d in state.selected}

    def work() -> str:
        kept_items, counts = [], {"supported": 0, "unsupported": 0, "contradicted": 0}
        for item in state.newsletter["items"]:
            kept = []
            for b in item["bullets"]:
                src = by_label[b["citation"]]
                res = ctx.llm.verify(b["text"], src["title"] + ". " + src["body"])
                verdict = res["verdict"] if res["verdict"] in counts else "unsupported"
                counts[verdict] += 1
                keep = verdict == "supported"
                state.claims.append(dict(label=b["citation"], document_id=src["id"], text=b["text"], verdict=verdict,
                                         score=float(res.get("score", 0.0)), reason=res.get("reason"), quote=res.get("quote"), kept=keep))
                if keep:
                    kept.append(b)
            if kept:
                kept_items.append({**item, "bullets": kept})
        total = sum(counts.values())
        state.removed = counts["unsupported"] + counts["contradicted"]
        for c in state.claims:
            conn.run("INSERT INTO newsletter_claims (id, draft_id, newsletter_id, document_id, label, claim_text, verdict, score, "
                     "reason, quote, kept) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (new_id(), state.draft_id, state.newsletter_id, c["document_id"], c["label"], c["text"], c["verdict"],
                      c["score"], c["reason"], c["quote"], 1 if c["kept"] else 0))
        conn.run("INSERT INTO verification_runs (id, draft_id, claims_total, supported, unsupported, contradicted, removed, status, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (new_id(), state.draft_id, total, counts["supported"], counts["unsupported"], counts["contradicted"],
                  state.removed, "ok" if kept_items else "all_removed", iso(ctx.clock.now())))
        if not kept_items:
            raise PipelineError("verification", "every claim failed verification against its source; nothing safe to send")
        state.newsletter = {**state.newsletter, "items": kept_items}
        return f"{counts['supported']}/{total} claims supported, {state.removed} removed"
    _audit(state, ctx, "verifier", work, 0, f"{len(state.newsletter['items'])} items", ctx.llm.model, ctx.llm.prompt_version)
    return None


def node_risk(state: DraftState, conn: Conn, ctx) -> str | None:
    def work() -> str:
        total = len(state.claims)
        state.risk, state.risk_reasons = assess_risk(state.selected, state.removed, total)
        return f"risk={state.risk} {state.risk_reasons}"
    _audit(state, ctx, "risk_reviewer", work, 0, "selected docs + verification", None, None)
    return None


def node_decision(state: DraftState, conn: Conn, ctx) -> str | None:
    def work() -> str:
        s = ctx.settings
        state.decision, state.reason = decide(state.risk, state.relevance, len(state.selected), s.relevance_min, s.relevance_high)
        if state.risk_reasons and state.decision == "review":
            state.reason += " (" + "; ".join(state.risk_reasons) + ")"
        return f"{state.decision}: {state.reason}"
    _audit(state, ctx, "decision", work, 0, f"risk={state.risk} relevance={state.relevance} n={len(state.selected)}", None, None)
    return "end" if state.decision == "skip" else None


def node_personaliser(state: DraftState, conn: Conn, ctx) -> str | None:
    def work() -> str:
        sources = {d["label"]: {"title": d["title"], "url": d["url"], "source_name": d["source_name"]} for d in state.selected}
        url = unsubscribe_url(ctx.settings, state.subscriber["id"])
        state.subject, state.text, state.html = render_newsletter(state.newsletter, sources, state.subscriber["name"],
                                                                 state.subscriber["language"], url)
        return f"rendered {len(state.html)} bytes html"
    _audit(state, ctx, "personaliser", work, 0, "validated newsletter", None, None)
    return None


NODES = [node_curator, node_summariser, node_verifier, node_risk, node_decision, node_personaliser]


def run_graph(state: DraftState, conn: Conn, ctx) -> DraftState:
    for node in NODES:
        if node(state, conn, ctx) == "end":
            break
    return state
