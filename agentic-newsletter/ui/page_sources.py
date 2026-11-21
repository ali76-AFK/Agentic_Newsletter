import pandas as pd
import streamlit as st

import common as c

st.title("📡 Sources and topics")
c.show_flash()

sources = c.load("/api/sources")
topics = c.load("/api/topics")
cfg = c.load("/api/diagnostics").get("config", {})
mode = cfg.get("feed_mode", "?")

st.write(f"Article mode: **{mode}**. Only enabled sources whose mode is '{mode}' are fetched "
         f"(articles older than {cfg.get('feed_freshness_days', '?')} days are dropped).")
if mode == "fixture":
    st.info("Demo mode uses built-in sample articles. To use real feeds set FEED_MODE=live in .env, then run ./run.sh up.")

st.dataframe(pd.DataFrame([{
    "Source": s["name"], "Key": s["source_key"], "Language": s["language"], "Mode": s["mode"], "Quality": s["quality"],
    "Enabled": bool(s["enabled"]), "Fetched now": bool(s["enabled"]) and s["mode"] == mode, "Feed": s["feed_url"]} for s in sources]),
    hide_index=True, width="stretch")
st.caption("To test that live feeds are reachable from your computer, run ./run.sh check-feeds in a terminal.")

if st.button("📥 Fetch new articles now"):
    with st.spinner("Fetching feeds..."):
        ok, res = c.call(c.api.post, "/api/ingest", timeout=180)
    if ok:
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("Sources", res["sources"])
        r2.metric("Articles seen", res["fetched"])
        r3.metric("New", res["new"])
        r4.metric("Already known", res["duplicate"])
        r5.metric("Too old", res["too_old"])
        for key, why in (res.get("errors") or {}).items():
            st.error(f"{key}: {why}")

st.subheader("Topics")
st.dataframe(pd.DataFrame([{"Key": t["topic_key"], "Topic": t["label"], "Active": bool(t["is_active"])} for t in topics]),
             hide_index=True, width="stretch")
