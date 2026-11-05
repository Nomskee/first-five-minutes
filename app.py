"""First Five Minutes: pick or paste a support ticket and see what the checks find."""
import json
import re
from pathlib import Path

import streamlit as st

from ffm.classify import classify
from ffm.diagnose import FLAG_LOOKUP_WARNING, FOOTER, diagnose

TICKETS_FILE = Path(__file__).parent / "tickets.json"
PLANS = ["free", "paid", "enterprise"]
STATUS_ICONS = {"fail": "🔴", "warn": "🟠", "pass": "🟢", "info": "🔵", "error": "⚪"}


def load_tickets():
    """Read the example tickets."""
    return json.loads(TICKETS_FILE.read_text(encoding="utf-8"))


def plain(text):
    """Stop Streamlit treating text as formatting. For example, it would read $pageview as the start of a formula."""
    return re.sub(r"([\\`*_{}\[\]$~#|])", r"\\\1", text)


def show_sidebar(tickets):
    """Draw the sidebar. Returns the ticket to look at, and whether Run diagnosis was clicked."""
    with st.sidebar:
        chosen = st.selectbox("Ticket", tickets, format_func=lambda t: f"{t['id']} – {t['subject']}")

        st.subheader("Or paste a new ticket")
        subject = st.text_input("Subject").strip()
        body = st.text_area("Body").strip()
        plan = st.selectbox("Plan", PLANS)
        st.caption("A pasted ticket is used instead of the dropdown. Clear it to go back.")

        clicked = st.button("Run diagnosis", type="primary")

    if subject or body:
        return {"id": "New", "plan": plan, "subject": subject, "body": body}, clicked
    return chosen, clicked


def show_ticket(ticket, classification):
    """Draw the ticket and its three labels at the top of the page."""
    st.subheader(plain(ticket["subject"] or "No subject"))
    # Two spaces before a line break keep the customer's line breaks on the page.
    st.markdown(plain(ticket["body"]).replace("\n", "  \n"))

    labels = {
        "Product area": classification["area"],
        "Owning team": classification["team"],
        "Severity": classification["severity"],
    }
    # Not st.metric, because its big text cuts off a value like "Client Libraries (SDKs)".
    for column, (label, value) in zip(st.columns(3), labels.items()):
        column.caption(label)
        column.markdown(f"**{plain(value)}**")


def likely_cause_text(note):
    """Take the Likely cause section from the note, so the box and the note always say the same thing."""
    section = note.split("\n### Likely cause\n", 1)[1]
    return section.split("\n### Evidence\n", 1)[0].strip()


def show_likely_cause(diagnosis):
    """Draw the highlighted box. Red only when a check that matters for this ticket failed."""
    st.subheader("Likely cause")
    # C9 and C10 always count as relevant, so there's always a top result.
    top = next(result for result in diagnosis["results"] if result.relevant)
    box = st.error if top.status == "fail" else st.info
    box(likely_cause_text(diagnosis["note_markdown"]).replace("$", "\\$"))


def show_card(result):
    """Draw one check as a card that opens to show the details."""
    with st.expander(f"{STATUS_ICONS[result.status]} {result.id} {plain(result.headline)}"):
        if result.search_terms:
            st.markdown(f"**Searched GitHub for:** {plain(', '.join(result.search_terms))}")
        if not result.evidence.empty:
            st.dataframe(result.evidence, hide_index=True)
        # C9 and C10 have no likely cause, and C10 has no query, so only show what's there.
        if result.likely_cause:
            st.markdown(f"**Likely cause:** {plain(result.likely_cause)}")
        if result.next_step:
            st.markdown(f"**Next step:** {plain(result.next_step)}")
        if result.docs_url:
            st.markdown(f"[Open docs]({result.docs_url})")
        if result.query:
            st.code(result.query, language="sql")
        st.caption(f"{result.seconds:.2f} seconds. Retried: {'yes' if result.retried else 'no'}.")


def show_diagnosis(diagnosis):
    """Draw everything the checks found."""
    if diagnosis["classification"]["flag_lookup_failed"]:
        st.warning(FLAG_LOOKUP_WARNING)
    st.caption(f"Total: {diagnosis['seconds']:.2f} seconds")
    show_likely_cause(diagnosis)

    st.subheader("Checks")
    for result in diagnosis["results"]:
        if result.relevant:
            show_card(result)

    st.subheader("Also found")
    # A check that errored might have found something, so it always gets a card.
    also_found = [r for r in diagnosis["results"] if not r.relevant and r.status in ("fail", "warn", "error")]
    for result in also_found:
        show_card(result)
    if not also_found:
        st.caption("Nothing else failed.")

    st.subheader("Internal note")
    st.code(diagnosis["note_markdown"], language="markdown")
    st.caption(FOOTER)


st.set_page_config(page_title="First Five Minutes", layout="wide")
st.title("First Five Minutes")

ticket, run_clicked = show_sidebar(load_tickets())

# Only use a saved diagnosis if it's for this ticket, so one ticket's findings never show under another.
saved = st.session_state.get("diagnosis")
diagnosis = saved if saved and saved["ticket"] == ticket else None

show_ticket(ticket, diagnosis["classification"] if diagnosis else classify(ticket))

if run_clicked:
    with st.spinner("Running checks…"):
        diagnosis = diagnose(ticket)
    # Streamlit reruns this whole file on every click, and session_state is what survives that.
    st.session_state["diagnosis"] = diagnosis

if diagnosis:
    show_diagnosis(diagnosis)
else:
    st.caption("Click Run diagnosis to check this ticket.")
