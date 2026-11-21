"""Web UI entry point (Streamlit). All data comes from the API; this container has no database access and no secrets."""
import os

import streamlit as st

st.set_page_config(page_title="Agentic Newsletter", page_icon="📰", layout="wide")

pages = [
    st.Page("page_overview.py", title="Overview", icon="📊", url_path="overview", default=True),
    st.Page("page_subscribers.py", title="Subscribers", icon="👥", url_path="subscribers"),
    st.Page("page_schedules.py", title="Schedules", icon="⏰", url_path="schedules"),
    st.Page("page_review.py", title="Review queue", icon="📝", url_path="review"),
    st.Page("page_history.py", title="History", icon="📚", url_path="history"),
    st.Page("page_sources.py", title="Sources", icon="📡", url_path="sources"),
    st.Page("page_system.py", title="System", icon="🛠️", url_path="system"),
]
nav = st.navigation(pages)

with st.sidebar:
    st.caption("Agentic AI Newsletter")
    mailpit = os.environ.get("UI_MAILPIT_URL", "")
    docs = os.environ.get("UI_API_DOCS_URL", "")
    if mailpit:
        st.link_button("📬 Open Mailpit (demo inbox)", mailpit)
    if docs:
        st.link_button("🔌 API documentation", docs)
    if st.button("🔄 Refresh data"):
        st.rerun()

nav.run()
