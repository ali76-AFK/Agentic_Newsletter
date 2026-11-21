from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

import common as c

st.title("📊 Overview")
c.show_flash()

diag = c.load("/api/diagnostics")
cfg = diag.get("config", {})
tz = c.zone(cfg.get("timezone"))
subs = c.load("/api/subscribers")
scheds = c.load("/api/schedules")
runs = c.load("/api/runs")
review = c.load("/api/review")
editions = c.load("/api/editions")
now = datetime.now(timezone.utc)
sched_name = {s["id"]: s["name"] for s in scheds}

# ---- system strip
alive, beat_text = c.worker_state(diag, now)
k1, k2, k3, k4 = st.columns(4)
k1.metric("Worker", "running" if alive else "NOT RESPONDING", beat_text, delta_color="normal" if alive else "inverse")
k2.metric("Email mode", "Demo (Mailpit)" if cfg.get("email_mode") == "mailpit" else "REAL SMTP")
k3.metric("Articles", cfg.get("feed_mode", "?"))
k4.metric("AI provider", cfg.get("llm_provider", "?"))
if cfg.get("email_mode") == "smtp":
    st.warning("Real email mode is ON: approving a draft or using 'Run now' sends real mail (only to addresses on the allowlist).")
else:
    st.info("Demo mode: every email lands in Mailpit on this computer. Nothing is sent to the internet.")
if not alive:
    st.error("The worker has not reported recently, so scheduled newsletters will not go out. In a terminal run: ./run.sh logs worker")

# ---- KPIs
week_ago = now - timedelta(days=7)
runs_7d = [r for r in runs if (c.parse_ts(r["started_at"]) or now) >= week_ago]
problems = [r for r in runs_7d if r["status"] in ("failed", "partial_failure")]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Active subscribers", sum(1 for s in subs if s["is_active"]), f"of {len(subs)} total", delta_color="off")
m2.metric("Enabled schedules", sum(1 for s in scheds if s["enabled"]), f"of {len(scheds)} total", delta_color="off")
m3.metric("Waiting for review", len(review), "needs you" if review else "all clear", delta_color="inverse" if review else "off")
m4.metric("Runs in last 7 days", len(runs_7d), f"{len(problems)} with problems" if problems else "no problems",
          delta_color="inverse" if problems else "off")
if review:
    try:
        st.page_link("page_review.py", label=f"Open the review queue ({len(review)} waiting)", icon="📝")
    except Exception:  # noqa: BLE001 - a navigation shortcut must never break the page
        st.caption("Open 'Review queue' in the left menu.")

# ---- charts
left, right = st.columns(2)
with left:
    st.subheader("Runs per day (last 14 days)")
    rows = []
    cutoff = now - timedelta(days=14)
    for r in runs:
        dt = c.parse_ts(r["started_at"])
        if dt and dt >= cutoff:
            rows.append({"day": dt.astimezone(tz).date().isoformat(), "status": r["status"]})
    if rows:
        df = pd.DataFrame(rows)
        domain = list(c.STATUS_COLOR)
        chart = alt.Chart(df).mark_bar().encode(
            x=alt.X("day:O", title=None), y=alt.Y("count():Q", title="runs", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("status:N", scale=alt.Scale(domain=domain, range=[c.STATUS_COLOR[k] for k in domain])),
            tooltip=["day", "status", "count()"])
        st.altair_chart(chart, width="stretch")
    else:
        st.caption("No runs yet. Create a schedule and press 'Run now' to see data here.")

drafts = []
ed_rows = []
for e in editions[:10]:
    try:
        detail = c.api.get(f"/api/editions/{e['id']}")
    except c.ApiError:
        continue
    ds = detail.get("drafts", [])
    drafts += ds
    rel = [d["relevance"] for d in ds if d.get("relevance") is not None]
    if rel:
        ed_rows.append({"edition": c.fmt_ts(e["created_at"], tz), "average relevance": round(sum(rel) / len(rel), 2)})

with right:
    st.subheader("What the agent decided (last 10 editions)")
    if drafts:
        dec = pd.DataFrame(drafts)
        dec["decision"] = dec["decision"].fillna("none")
        counts = dec.groupby("decision").size().reset_index(name="emails")
        chart = alt.Chart(counts).mark_arc(innerRadius=50).encode(
            theta="emails:Q", color=alt.Color("decision:N", legend=alt.Legend(title=None)), tooltip=["decision", "emails"])
        st.altair_chart(chart, width="stretch")
        st.caption("auto_send = sent automatically, review = a human must approve, skip = not relevant enough.")
    else:
        st.caption("No emails generated yet.")

if ed_rows:
    st.subheader("Average relevance per edition")
    st.line_chart(pd.DataFrame(ed_rows).set_index("edition"), height=220)

# ---- upcoming + recent
a, b = st.columns(2)
with a:
    st.subheader("Next planned deliveries")
    up = []
    for s in scheds:
        if not s["enabled"]:
            continue
        n = c.next_run(s, now)
        if n:
            up.append({"when": n, "Schedule": s["name"], "Next delivery": n.astimezone(c.zone(s["timezone"])).strftime("%a %Y-%m-%d %H:%M ") + s["timezone"],
                       "In": c.until(n, now)})
    if up:
        st.dataframe(pd.DataFrame(sorted(up, key=lambda x: x["when"])).drop(columns="when"), hide_index=True, width="stretch")
    else:
        st.caption("No enabled schedules. Add one on the Schedules page.")
with b:
    st.subheader("Recent runs")
    if runs:
        st.dataframe(pd.DataFrame([{"Started": c.fmt_ts(r["started_at"], tz), "Schedule": sched_name.get(r["schedule_id"], "?"),
                                    "Status": c.status_label(r["status"]), "Result": r.get("message") or ""} for r in runs[:8]]),
                     hide_index=True, width="stretch")
    else:
        st.caption("Nothing has run yet.")

# ---- action
st.divider()
st.subheader("Quick action")
if st.button("📥 Fetch new articles now"):
    with st.spinner("Fetching feeds..."):
        ok, res = c.call(c.api.post, "/api/ingest", timeout=180)
    if ok:
        errs = res.get("errors") or {}
        c.flash("warning" if errs else "success",
                f"Fetched {res['fetched']} articles from {res['sources']} sources: {res['new']} new, {res['duplicate']} already known, "
                f"{res['too_old']} too old." + (f" Problems: {errs}" if errs else ""))
        st.rerun()
