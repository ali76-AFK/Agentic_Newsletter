from datetime import datetime, time, timezone
from zoneinfo import available_timezones

import pandas as pd
import streamlit as st

import common as c

st.title("⏰ Schedules")
c.show_flash()

scheds = c.load("/api/schedules")
topics = c.load("/api/topics")
sources = c.load("/api/sources")
diag = c.load("/api/diagnostics")
cfg = diag.get("config", {})
default_tz = cfg.get("timezone") or "UTC"
real_mail = cfg.get("email_mode") == "smtp"
now = datetime.now(timezone.utc)
topic_label = {t["topic_key"]: t["label"] for t in topics if t["is_active"]}
src_name = {s["source_key"]: s["name"] for s in sources}
tz_names = sorted(available_timezones())
if default_tz not in tz_names:
    tz_names.insert(0, default_tz)


def schedule_fields(prefix: str, cur: dict | None):
    """Shared widgets for create and edit. Must be called inside a form."""
    cur = cur or {}
    c1, c2, c3 = st.columns([2, 1, 2])
    name = c1.text_input("Name", cur.get("name", ""), key=f"{prefix}_name")
    h, m = (int(x) for x in cur.get("delivery_time", "07:30").split(":"))
    at = c2.time_input("Delivery time", time(h, m), key=f"{prefix}_time", step=300)
    tzn = c3.selectbox("Timezone", tz_names, index=tz_names.index(cur.get("timezone", default_tz)) if cur.get("timezone", default_tz) in tz_names else 0,
                       key=f"{prefix}_tz")
    days = st.multiselect("Days", range(7), default=c.jlist(cur.get("weekdays_json")) or list(range(7)),
                          format_func=lambda d: c.WEEKDAYS[d], key=f"{prefix}_days")
    tps = st.multiselect("Topics (empty = every topic a subscriber follows)", list(topic_label),
                         default=[t for t in c.jlist(cur.get("topics_json")) if t in topic_label],
                         format_func=lambda k: topic_label[k], key=f"{prefix}_topics")
    srcs = st.multiselect("Sources (empty = all)", list(src_name), default=[s for s in c.jlist(cur.get("source_keys_json")) if s in src_name],
                          format_func=lambda k: src_name[k], key=f"{prefix}_srcs")
    return {"name": name, "delivery_time": at.strftime("%H:%M"), "timezone": tzn, "weekdays": sorted(days), "topics": tps, "source_keys": srcs}


with st.expander("➕ Create a schedule", expanded=not scheds):
    with st.form("create_schedule", clear_on_submit=False):
        body = schedule_fields("new", None)
        if st.form_submit_button("Create schedule"):
            if not body["name"].strip():
                st.error("Please give the schedule a name.")
            elif not body["weekdays"]:
                st.error("Select at least one day.")
            else:
                ok, _ = c.call(c.api.post, "/api/schedules", body)
                if ok:
                    c.flash("success", f"Schedule '{body['name']}' created.")
                    st.rerun()

if not scheds:
    st.info("No schedules yet. Create one above, then press 'Run now' to try it immediately.")
    st.stop()

rows = []
for s in scheds:
    n = c.next_run(s, now) if s["enabled"] else None
    rows.append({"Name": s["name"], "Enabled": bool(s["enabled"]), "Time": f"{s['delivery_time']} {s['timezone']}",
                 "Days": c.weekdays_text(c.jlist(s["weekdays_json"])),
                 "Topics": ", ".join(topic_label.get(t, t) for t in c.jlist(s["topics_json"])) or "all",
                 "Next delivery": n.astimezone(c.zone(s["timezone"])).strftime("%a %Y-%m-%d %H:%M") if n else "-"})
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

st.divider()
st.subheader("Manage a schedule")
by_id = {s["id"]: s for s in scheds}
sid = st.selectbox("Choose", list(by_id), format_func=lambda i: by_id[i]["name"] + ("" if by_id[i]["enabled"] else "  - disabled"))
cur = by_id[sid]

with st.form(f"edit_{sid}"):
    body = schedule_fields(f"e_{sid}", cur)
    enabled = st.toggle("Enabled", value=bool(cur["enabled"]), key=f"e_{sid}_enabled")
    if st.form_submit_button("Save changes"):
        if not body["weekdays"]:
            st.error("Select at least one day.")
        else:
            ok, _ = c.call(c.api.patch, f"/api/schedules/{sid}", {**body, "enabled": enabled})
            if ok:
                c.flash("success", "Schedule saved.")
                st.rerun()

st.subheader("Run now")
st.caption("Runs the whole pipeline immediately for every matching subscriber, outside the normal schedule.")
go = True
if real_mail:
    go = st.checkbox("I understand this sends REAL emails to subscribers on the allowlist", key=f"real_{sid}")
if st.button("▶️ Run now", key=f"run_{sid}", disabled=not go):
    with st.spinner("Running the pipeline (this can take a while with a real AI model)..."):
        ok, res = c.call(c.api.post, f"/api/schedules/{sid}/run-now", timeout=300)
    if ok:
        c.flash("success" if res["status"] == "sent" else "warning", f"Run finished: {res['status']} - {res['message']}")
        st.rerun()

with st.expander("Run history of this schedule"):
    hist = c.load(f"/api/schedules/{sid}/runs")
    tz = c.zone(default_tz)
    if hist:
        st.dataframe(pd.DataFrame([{"Started": c.fmt_ts(r["started_at"], tz), "Status": c.status_label(r["status"]),
                                    "Result": r.get("message") or "", "Planned for": c.fmt_ts(r.get("scheduled_for"), tz) or "manual"} for r in hist]),
                     hide_index=True, width="stretch")
    else:
        st.caption("This schedule has not run yet.")
