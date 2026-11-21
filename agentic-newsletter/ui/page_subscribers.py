import pandas as pd
import streamlit as st

import common as c

st.title("👥 Subscribers")
c.show_flash()

subs = c.load("/api/subscribers")
topics = c.load("/api/topics")
sources = c.load("/api/sources")
diag = c.load("/api/diagnostics")
tz = c.zone(diag.get("config", {}).get("timezone"))
topic_label = {t["topic_key"]: t["label"] for t in topics if t["is_active"]}
default_lang = diag.get("config", {}).get("default_language", "en")

m1, m2, m3 = st.columns(3)
active = [s for s in subs if s["is_active"]]
m1.metric("Active", len(active))
m2.metric("English / German", f"{sum(1 for s in active if s['language'] == 'en')} / {sum(1 for s in active if s['language'] == 'de')}")
m3.metric("Inactive (unsubscribed)", len(subs) - len(active))

with st.expander("➕ Add a subscriber", expanded=not subs):
    with st.form("add_subscriber", clear_on_submit=True):
        c1, c2 = st.columns(2)
        email = c1.text_input("Email address")
        name = c2.text_input("Name")
        lang = st.radio("Newsletter language", ["en", "de"], index=0 if default_lang != "de" else 1, horizontal=True,
                        format_func=lambda x: {"en": "English", "de": "Deutsch"}[x])
        chosen = st.multiselect("Topics", list(topic_label), format_func=lambda k: topic_label[k])
        if st.form_submit_button("Add subscriber"):
            if not email.strip() or not name.strip():
                st.error("Email and name are required.")
            else:
                ok, _ = c.call(c.api.post, "/api/subscribers", {"email": email, "name": name, "language": lang, "topics": chosen})
                if ok:
                    c.flash("success", f"Added {email.strip().lower()}.")
                    st.rerun()

if not subs:
    st.info("No subscribers yet. Add the first one above.")
    st.stop()

st.dataframe(pd.DataFrame([{
    "Email": s["email"], "Name": s["name"], "Language": s["language"], "Active": bool(s["is_active"]),
    "Topics": ", ".join(topic_label.get(t, t) for t in s["topics"]), "Since": c.fmt_ts(s["created_at"], tz)} for s in subs]),
    hide_index=True, width="stretch")

counts = {}
for s in active:
    for t in s["topics"]:
        counts[topic_label.get(t, t)] = counts.get(topic_label.get(t, t), 0) + 1
if counts:
    st.subheader("Topic popularity (active subscribers)")
    st.bar_chart(pd.Series(counts, name="subscribers").sort_values(ascending=False), height=240)

st.divider()
st.subheader("Edit a subscriber")
by_id = {s["id"]: s for s in subs}
sid = st.selectbox("Choose", list(by_id), format_func=lambda i: f"{by_id[i]['email']}  ({by_id[i]['name']})"
                   + ("" if by_id[i]["is_active"] else "  - inactive"))
cur = by_id[sid]

with st.form(f"edit_{sid}"):
    c1, c2 = st.columns(2)
    new_name = c1.text_input("Name", cur["name"])
    new_lang = c2.radio("Language", ["en", "de"], index=0 if cur["language"] == "en" else 1, horizontal=True,
                        format_func=lambda x: {"en": "English", "de": "Deutsch"}[x])
    new_topics = st.multiselect("Topics", list(topic_label), default=[t for t in cur["topics"] if t in topic_label],
                                format_func=lambda k: topic_label[k])
    new_active = st.toggle("Active (receives newsletters)", value=bool(cur["is_active"]))
    if st.form_submit_button("Save changes"):
        ok, _ = c.call(c.api.patch, f"/api/subscribers/{sid}",
                       {"name": new_name, "language": new_lang, "topics": new_topics, "is_active": new_active})
        if ok:
            c.flash("success", f"Saved {cur['email']}.")
            st.rerun()

with st.expander("Delivery preferences"):
    try:
        prefs = c.api.get(f"/api/subscribers/{sid}/preferences")
    except c.ApiError as exc:
        prefs = None
        st.error(str(exc))
    if prefs:
        styles = ["concise", "detailed", "casual", "formal"]
        cur_style = prefs.get("summary_style") or "concise"
        if cur_style not in styles:
            styles.append(cur_style)
        known_src = {s["source_key"]: s["name"] for s in sources}
        with st.form(f"prefs_{sid}"):
            p1, p2, p3 = st.columns(3)
            max_a = p1.number_input("Max articles per email", 1, 20, int(prefs["max_articles"]))
            look = p2.number_input("Look back (hours)", 1, 1440, int(prefs["lookback_hours"]))
            style = p3.selectbox("Summary style", styles, index=styles.index(cur_style))
            excl = st.text_input("Exclude keywords (comma separated)", ", ".join(c.jlist(prefs.get("excluded_keywords_json"))))
            cur_src = [k for k in c.jlist(prefs.get("source_keys_json")) if k in known_src]
            src = st.multiselect("Only use these sources (empty = all)", list(known_src), default=cur_src, format_func=lambda k: known_src[k])
            if st.form_submit_button("Save preferences"):
                ok, _ = c.call(c.api.put, f"/api/subscribers/{sid}/preferences", {
                    "max_articles": int(max_a), "lookback_hours": int(look), "summary_style": style,
                    "excluded_keywords": [x.strip() for x in excl.split(",") if x.strip()], "source_keys": src})
                if ok:
                    c.flash("success", "Preferences saved.")
                    st.rerun()

if cur["is_active"]:
    with st.expander("Danger zone"):
        st.caption("Deactivating stops all emails to this person. History is kept. You can re-activate above.")
        if st.checkbox("Yes, deactivate this subscriber", key=f"confirm_{sid}") and st.button("Deactivate", key=f"deact_{sid}"):
            ok, _ = c.call(c.api.delete, f"/api/subscribers/{sid}")
            if ok:
                c.flash("success", f"{cur['email']} deactivated.")
                st.rerun()
