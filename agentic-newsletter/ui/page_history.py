from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

import common as c

st.title("📚 History")
c.show_flash()

runs = c.load("/api/runs")
editions = c.load("/api/editions")
scheds = c.load("/api/schedules")
diag = c.load("/api/diagnostics")
tz = c.zone(diag.get("config", {}).get("timezone"))
sched_name = {s["id"]: s["name"] for s in scheds}
run_by_id = {r["id"]: r for r in runs}

tab_ed, tab_runs = st.tabs(["Editions (what was generated)", "Runs (what the scheduler did)"])

with tab_ed:
    if not editions:
        st.info("No editions yet. An edition is created every time a schedule runs.")
    else:
        def label(e):
            r = run_by_id.get(e["schedule_run_id"], {})
            return f"{c.fmt_ts(e['created_at'], tz)}  -  {sched_name.get(r.get('schedule_id'), 'schedule')}  -  {e['status']}"
        by_id = {e["id"]: e for e in editions}
        eid = st.selectbox("Edition", list(by_id), format_func=lambda i: label(by_id[i]))
        detail = c.load(f"/api/editions/{eid}")
        drafts = detail.get("drafts", [])
        st.write(f"**Status:** {c.status_label(detail['status'])}    **Emails:** {len(drafts)}")
        if drafts:
            st.dataframe(pd.DataFrame([{
                "Recipient": d["recipient_email"], "Status": c.status_label(d["status"]),
                "Relevance": d.get("relevance"), "Risk": d.get("risk") or "", "Decision": d.get("decision") or "",
                "Reason": d.get("decision_reason") or "", "Error": (f"[{d['error_stage']}] " if d.get("error_stage") else "") + (d.get("error") or ""),
                "Sent": c.fmt_ts(d.get("sent_at"), tz)} for d in drafts]),
                hide_index=True, width="stretch",
                column_config={"Relevance": st.column_config.ProgressColumn("Relevance", min_value=0.0, max_value=1.0, format="%.2f")})
            failed = [d for d in drafts if d["status"] == "failed"]
            if failed:
                st.error(f"{len(failed)} email(s) failed to send. Recipients who already received it will NOT be contacted again.")
                if st.button("🔁 Retry failed emails", key=f"retry_{eid}"):
                    with st.spinner("Retrying..."):
                        ok, res = c.call(c.api.post, f"/api/editions/{eid}/retry", timeout=180)
                    if ok:
                        c.flash("success" if res["status"] == "sent" else "warning", f"Retry finished: {res['status']} - {res['message']}")
                        st.rerun()

with tab_runs:
    if not runs:
        st.info("No runs yet.")
    else:
        statuses = sorted({r["status"] for r in runs})
        pick = st.multiselect("Show statuses", statuses, default=statuses)
        rows = []
        for r in runs:
            if r["status"] not in pick:
                continue
            s, e = c.parse_ts(r["started_at"]), c.parse_ts(r.get("completed_at"))
            rows.append({"Started": c.fmt_ts(r["started_at"], tz), "Schedule": sched_name.get(r["schedule_id"], "?"),
                         "Status": c.status_label(r["status"]), "Seconds": int((e - s).total_seconds()) if s and e else None,
                         "Kind": "manual" if str(r["run_key"]).startswith("manual:") else "scheduled", "Result": r.get("message") or ""})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        running = [r for r in runs if r["status"] == "running"]
        if running:
            st.warning(f"{len(running)} run(s) show 'running'. If this stays for more than a few minutes the worker died mid-run: "
                       "check ./run.sh logs worker. Nothing is re-sent automatically.")
        counts = pd.DataFrame(rows).groupby("Status").size().reset_index(name="runs") if rows else None
        if counts is not None and len(counts):
            st.bar_chart(counts.set_index("Status"), height=220)
