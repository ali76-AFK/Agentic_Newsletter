import streamlit as st

import common as c

st.title("📝 Review queue")
c.show_flash()

queue = c.load("/api/review")
diag = c.load("/api/diagnostics")
cfg = diag.get("config", {})
tz = c.zone(cfg.get("timezone"))
real_mail = cfg.get("email_mode") == "smtp"

st.caption("Drafts the agent was not sure about (medium/high risk or only moderately relevant). Nothing here has been sent. "
           "Approve sends the email to that one person; Reject keeps it unsent.")
if not queue:
    st.success("Nothing is waiting for review.")
    st.stop()

st.metric("Waiting", len(queue))
for d in queue:
    did = d["id"]
    title = f"{d['recipient_email']}  -  {d.get('subject') or '(no subject)'}"
    with st.expander(title, expanded=len(queue) == 1):
        a, b, cc = st.columns(3)
        a.metric("Relevance", f"{d['relevance']:.2f}" if d.get("relevance") is not None else "-")
        b.metric("Risk", d.get("risk") or "-")
        cc.metric("Created", c.fmt_ts(d.get("created_at"), tz))
        st.write(f"**Why it is here:** {d.get('decision_reason') or '-'}")
        st.text_area("Email text", d.get("text_body") or "", height=260, disabled=True, key=f"body_{did}")

        ok_to_send = True
        if real_mail:
            ok_to_send = st.checkbox("I understand approving sends a REAL email", key=f"real_{did}")
        b1, b2 = st.columns([1, 3])
        if b1.button("✅ Approve and send", key=f"approve_{did}", disabled=not ok_to_send):
            with st.spinner("Sending..."):
                ok, res = c.call(c.api.post, f"/api/review/{did}/approve", timeout=120)
            if ok:
                c.flash("success" if res["status"] == "sent" else "warning", f"{d['recipient_email']}: {res['status']}")
                st.rerun()
        reason = b2.text_input("Reason for rejecting", key=f"reason_{did}", placeholder="required to reject")
        if b2.button("🚫 Reject", key=f"reject_{did}"):
            if not reason.strip():
                st.error("Please enter a reason first.")
            else:
                ok, _ = c.call(c.api.post, f"/api/review/{did}/reject", {"reason": reason})
                if ok:
                    c.flash("info", f"Rejected the draft for {d['recipient_email']}.")
                    st.rerun()
