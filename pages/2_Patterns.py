"""Patterns: checks that keep failing across tickets, and a draft GitHub issue for one of them.

This reads our own telemetry (ffm_ events in the internal PostHog project), not the customer's data.
"""
import re
from collections import Counter
from urllib.parse import quote

import pandas as pd
import streamlit as st

from ffm.classify import AREA_KEYWORDS
from ffm.config import ANCHOR_MODE, GITHUB_USERNAME, INTERNAL_PROJECT_ID
from ffm.diagnose import AREA_CHECKS
from ffm.github_lookup import FIXED_SEARCH_WORDS, POSTHOG_SDK_NAMES, check_c9_github
from ffm.posthog_api import run_hogql

# Checks that failed in this ticket's area on two or more tickets in the last 7 days.
# A ticket is counted by ticket_key (a hash of its body), so one ticket run from both the app and Slack counts once.
# Runs from before ticket_key existed are left out, because they can't be matched up. So are test runs of the telemetry.
PATTERNS_QUERY = """SELECT properties.check_id AS check_id,
       count(DISTINCT properties.ticket_key) AS tickets,
       groupUniqArray(properties.ticket_id) AS ticket_ids
FROM events
WHERE event = 'ffm_check_result'
  AND properties.status = 'fail'
  AND properties.relevant = true
  AND properties.ticket_key IS NOT NULL
  AND properties.ticket_id NOT LIKE 'telemetry-test-%'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY check_id
HAVING tickets >= 2
ORDER BY tickets DESC"""

# "This week" ends at our newest ffm_ event, the same way the checks measure from the customer's newest event.
ANCHOR_QUERY = "SELECT max(timestamp) FROM events WHERE event LIKE 'ffm_%'"

# GitHub's new-issue link carries the body in the URL, so the encoded body has to stay under this.
MAX_ENCODED_BODY = 6000

# Re-run the queries at most once a minute. Without this, every click or keystroke on the page would query again.
CACHE_SECONDS = 60


def hogql_string(value):
    """Put text in quotes for a HogQL query, so a quote inside it can't end the string early."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


@st.cache_data(ttl=CACHE_SECONDS)
def get_anchor():
    """Return the newest ffm_ event's time, or None in "now" mode or if there are no ffm_ events yet."""
    if ANCHOR_MODE == "now":
        return None
    result = run_hogql(ANCHOR_QUERY, project_id=INTERNAL_PROJECT_ID)
    anchor = result.iloc[0, 0] if not result.empty else None
    # With no events, PostHog may answer with a blank or with the earliest possible date, 1970-01-01.
    if pd.isna(anchor) or str(anchor).startswith("1970-01-01"):
        return None
    return str(anchor)


def anchored(query, anchor):
    """Swap now() in a query for the anchor, if there is one."""
    return query if anchor is None else query.replace("now()", f"toDateTime('{anchor}')")


@st.cache_data(ttl=CACHE_SECONDS)
def run_query(query):
    """Run a HogQL query on the internal project."""
    return run_hogql(query, project_id=INTERNAL_PROJECT_ID)


def tickets_query(ticket_ids, anchor):
    """One row per ticket, grouped by ticket_key like the count, with all its ticket_ids and its newest subject and severity."""
    ids = ", ".join(hogql_string(id) for id in ticket_ids)
    return anchored(f"""SELECT properties.ticket_key AS ticket_key,
       groupUniqArray(properties.ticket_id) AS ticket_ids,
       argMax(properties.ticket_subject, timestamp) AS subject,
       argMax(properties.severity, timestamp) AS severity
FROM events
WHERE event = 'ffm_diagnostic_run'
  AND properties.ticket_key IS NOT NULL
  AND properties.ticket_id IN ({ids})
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY ticket_key
ORDER BY min(properties.ticket_id)""", anchor)


def latest_check_query(check_id, anchor):
    """The check's name, headline and likely cause from its newest relevant failure this week.

    Only events recorded after these fields were added have a headline, so older ones are skipped.
    """
    return anchored(f"""SELECT argMax(properties.check_name, timestamp) AS check_name,
       argMax(properties.headline, timestamp) AS headline,
       argMax(properties.likely_cause, timestamp) AS likely_cause
FROM events
WHERE event = 'ffm_check_result'
  AND properties.check_id = {hogql_string(check_id)}
  AND properties.status = 'fail'
  AND properties.relevant = true
  AND properties.headline IS NOT NULL
  AND properties.ticket_id NOT LIKE 'telemetry-test-%'
  AND timestamp > now() - INTERVAL 7 DAY""", anchor)


def search_terms(check_id, headline):
    """The safe GitHub search terms for a check: its fixed word, the SDK its headline names, or its area's keywords.

    check_c9_github() drops anything that isn't on the allowlist anyway, so nothing else can be sent.
    """
    if check_id in FIXED_SEARCH_WORDS:
        return [FIXED_SEARCH_WORDS[check_id]]
    # Longest names first, so "posthog-js-lite" isn't read as "posthog-js".
    for name in sorted(POSTHOG_SDK_NAMES, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", headline or ""):
            return [name]
    for area, checks in AREA_CHECKS.items():
        if check_id in checks:
            return AREA_KEYWORDS[area]
    return []


@st.cache_data(ttl=5 * CACHE_SECONDS)
def related_issues(terms):
    """Run C9 with these terms. Cached for longer, because GitHub only allows a few searches a minute."""
    return check_c9_github(list(terms))


def build_body(name, latest, tickets, query, fix, c9, max_subjects=None, include_query=True):
    """Write the issue body. max_subjects and include_query let it be shortened to fit in a link."""
    headline = latest.get("headline") or "_Not recorded. Only runs after telemetry started saving headlines have one._"
    cause = latest.get("likely_cause") or "_Not recorded._"
    lines = ["## Summary", "", f"**Latest headline:** {headline}", "", f"**Likely cause:** {cause}"]

    severities = Counter(tickets["severity"].fillna("unknown"))
    order = ["high", "medium", "low", "unknown"]
    severity_text = ", ".join(f"{level}: {severities[level]}" for level in order if severities[level])
    lines += ["", "## Customer impact", "", f"{name} failed on {len(tickets)} tickets this week.", "", f"**Severities:** {severity_text}", ""]
    shown = tickets if max_subjects is None else tickets.head(max_subjects)
    # A ticket run from both the app and Slack has two ids, so list them all.
    lines += [
        f"- {', '.join(f'`{id}`' for id in sorted(row.ticket_ids))} ({row.severity or 'unknown'}): {row.subject or 'No subject'}"
        for row in shown.itertuples()
    ]
    if len(shown) < len(tickets):
        lines.append(f"- …and {len(tickets) - len(shown)} more, left out so the link stays short enough.")

    lines += ["", "## Evidence", ""]
    if include_query:
        lines += ["Found with this HogQL query on the internal PostHog project:", "", "```sql", query, "```"]
    else:
        lines.append("_The HogQL query was left out so the link stays short enough. It's on the Patterns page._")

    lines += ["", "## Suggested fix", "", fix.strip() or "_Not written yet._"]

    lines += ["", "## Related issues", "", c9.headline]
    if c9.search_terms:
        lines += ["", f"Searched GitHub for: {', '.join(c9.search_terms)}"]
    if not c9.evidence.empty:
        lines.append("")
        lines += [f"- [#{row.number} {row.title}]({row.url}) ({row.state})" for row in c9.evidence.itertuples()]
    return "\n".join(lines)


def fits(body):
    return len(quote(body, safe="")) <= MAX_ENCODED_BODY


def body_for_link(name, latest, tickets, query, fix, c9):
    """Shorten the body until it fits in a link: subjects first, then the query. Returns (body, was it shortened?)."""
    full = build_body(name, latest, tickets, query, fix, c9)
    if fits(full):
        return full, False
    for count in range(len(tickets) - 1, -1, -1):
        body = build_body(name, latest, tickets, query, fix, c9, max_subjects=count)
        if fits(body):
            return body, True
    body = build_body(name, latest, tickets, query, fix, c9, max_subjects=0, include_query=False)
    if fits(body):
        return body, True
    # Last resort, for a very long suggested fix: cut the end off and say so.
    note = "\n\n_…cut short to fit in a link. The full draft is in the downloaded file._"
    while not fits(body + note):
        body = body[: int(len(body) * 0.9)]
    return body + note, True


st.set_page_config(page_title="Patterns", layout="wide")
st.title("Patterns")

anchor = get_anchor()
if anchor is None and ANCHOR_MODE != "now":
    st.info("No diagnoses have been recorded yet. Run a few from the main page or Slack first.")
    st.stop()

query = anchored(PATTERNS_QUERY, anchor)
patterns = run_query(query)

st.subheader("Repeated causes this week")
st.caption(f"Checks that failed in the ticket's own area on 2 or more tickets in the 7 days up to {anchor or 'now'}.")
if patterns.empty:
    st.info("No check failed on two or more tickets this week.")
    st.stop()
st.dataframe(patterns, hide_index=True)

check_id = st.selectbox("Check", patterns["check_id"].tolist())
row = patterns[patterns["check_id"] == check_id].iloc[0]

latest_rows = run_query(latest_check_query(check_id, anchor))
latest = latest_rows.iloc[0].to_dict() if not latest_rows.empty else {}
name = f"{check_id} {latest['check_name']}" if latest.get("check_name") else check_id
tickets = run_query(tickets_query(tuple(row["ticket_ids"]), anchor))

fix = st.text_area("Suggested fix")
c9 = related_issues(tuple(search_terms(check_id, latest.get("headline"))))

title = f"[Support pattern] {name}: seen in {int(row['tickets'])} tickets this week"
full_body = build_body(name, latest, tickets, query, fix, c9)
link_body, shortened = body_for_link(name, latest, tickets, query, fix, c9)
draft = f"# {title}\n\n{full_body}\n"

st.subheader("Issue draft")
st.code(draft, language="markdown")
if shortened:
    st.caption("The GitHub link carries a shorter version, so it fits in a URL. The download has everything.")

download, open_issue = st.columns(2)
download.download_button("Download .md", draft, file_name=f"support-pattern-{check_id}.md", mime="text/markdown")
if GITHUB_USERNAME:
    url = (
        f"https://github.com/{quote(GITHUB_USERNAME, safe='')}/first-five-minutes/issues/new"
        f"?title={quote(title, safe='')}&body={quote(link_body, safe='')}"
    )
    open_issue.link_button("Open a GitHub issue", url)
else:
    open_issue.warning("Add GITHUB_USERNAME to .env and restart the app to get a GitHub link.")
