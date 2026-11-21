import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import common as c

st.title("🛠️ System")
c.show_flash()

diag = c.load("/api/diagnostics")
cfg = diag.get("config", {})
now = datetime.now(timezone.utc)
alive, beat_text = c.worker_state(diag, now)

st.subheader("Health")
h1, h2, h3 = st.columns(3)
h1.metric("API", "reachable")
h2.metric("Worker", "running" if alive else "NOT RESPONDING", beat_text, delta_color="normal" if alive else "inverse")
h3.metric("Email mode", cfg.get("email_mode", "?"))

st.subheader("Mail server check")
if cfg.get("email_mode") != "smtp":
    st.info("Email mode is 'mailpit' (demo): nothing to check. Open the Mailpit link in the sidebar to see demo emails.")
elif st.button("Test the SMTP connection"):
    with st.spinner("Connecting..."):
        ok, res = c.call(c.api.post, "/api/diagnostics/smtp-check", timeout=60)
    if ok:
        (st.success if res.get("ok") else st.error)(str(res.get("info") if res.get("ok") else res.get("error")))

st.subheader("Configuration (secrets are hidden)")
st.dataframe(pd.DataFrame([{"Setting": k, "Value": str(v)} for k, v in cfg.items()]), hide_index=True, width="stretch")
st.caption("Settings live in the .env file. After changing it, run ./run.sh up to apply.")

st.subheader("This web interface")
st.write(f"API address used by this page: `{c.api.base}`")
st.caption("This interface has no login. It is only reachable from this computer (127.0.0.1) on purpose.")
