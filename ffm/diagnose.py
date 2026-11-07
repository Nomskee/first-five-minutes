"""Run the whole first-five-minutes diagnosis for one support ticket."""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from ffm.checks import ALL_CHECKS, EXPECTED_ERRORS, CheckResult, check_c5_flags, get_anchor
from ffm.classify import _fetch_flag_keys, classify
from ffm.config import CUSTOMER_PROJECT_ID
from ffm.github_lookup import FIXED_SEARCH_WORDS, POSTHOG_SDK_NAMES, check_c9_github, check_c10_docs
from ffm.posthog_api import clear_retried, was_retried
from ffm.telemetry import log_diagnosis

# How many checks can run at the same time.
MAX_WORKERS = 4

# Which checks belong to each area. C7 is in none, so it always shows under "Also found".
AREA_CHECKS = {
    "feature_flags": ["C5"],
    "error_tracking": ["C6"],
    "identification": ["C4"],
    "data_quality": ["C2", "C3"],
    "ingestion": ["C1", "C8"],
}

# The order to show statuses in. A failure in the ticket's area comes before all of these.
STATUS_ORDER = {"fail": 1, "warn": 2, "error": 3, "pass": 4, "info": 5}

# Added when C4 and C5 both fail on a ticket about one of them, because the same problem causes both.
SHARED_ROOT_CAUSE = "Shared root cause: users are never identified with their email."
LINKED_CHECKS = ("C4", "C5")
LINKED_AREAS = ("feature_flags", "identification")

# Where to find the SDK name, as (column, column that puts the worst row first).
SDK_COLUMNS = {"C1": ("lib", None), "C2": ("lib", "minors_behind"), "C4": ("lib", None)}

# How many evidence rows to show in the note for each check.
MAX_EVIDENCE_ROWS = 5

FOOTER = "Internal note. Write the customer reply yourself."

# Likely-cause lines that don't come from a check's own text.
NO_PROBLEM_FOUND = "No check in this ticket's area found a problem."
COULD_NOT_RUN = "This check couldn't run, so this part of the ticket wasn't checked."

# Shown in the app and the note when classify() couldn't get the flag keys.
FLAG_LOOKUP_WARNING = "Couldn't load feature flags, so the area and team may be wrong."


def _run_check(check, *args):
    """Run and time one check. Anything unexpected becomes an error result, so one broken check can't stop the others."""
    started = time.perf_counter()
    # Worker threads get reused, so forget any retry from the check this thread ran before.
    clear_retried()
    try:
        result = check(*args)
    except Exception as error:
        # A name like check_c5_flags gives the id C5.
        id = check.__name__.split("_")[1].upper()
        result = CheckResult(
            id, check.__name__, "error", f"{check.__name__} crashed: {error}", pd.DataFrame(),
            "The check failed before it could look at the data.",
            "Fix the error above and run the check again.", "", "",
        )
    result.seconds = round(time.perf_counter() - started, 2)
    result.retried = was_retried()
    return result


def _lookup_anchor():
    """Get a fresh anchor for this diagnosis, and return how many seconds it took."""
    started = time.perf_counter()
    # Looking it up now stops four threads all fetching it at once.
    # Pass the project the same way the checks do, because the cache treats get_anchor() as a different call.
    get_anchor.cache_clear()
    try:
        get_anchor(CUSTOMER_PROJECT_ID)
    except EXPECTED_ERRORS:
        pass  # each check will hit the same problem and report it
    return time.perf_counter() - started


def _run_checks(flag_key):
    """Run C1 to C8 at the same time, and return their results in order."""

    def run(check):
        return _run_check(check, flag_key) if check is check_c5_flags else _run_check(check)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        # map() returns the answers in the same order as ALL_CHECKS, whichever finishes first.
        return list(pool.map(run, ALL_CHECKS))


def _sort_results(results, area):
    """Sort: fails in the ticket's area, other fails, warns, errors, passes, then info."""
    in_area = AREA_CHECKS.get(area, [])

    def rank(result):
        return 0 if result.status == "fail" and result.id in in_area else STATUS_ORDER[result.status]

    # sorted() keeps equal results in their current order, so each group stays in check order.
    return sorted(results, key=rank)


def _search_word(result):
    """Return the word to add to the GitHub search for a check's result, or None if there isn't a safe one."""
    if result.id in FIXED_SEARCH_WORDS:
        return FIXED_SEARCH_WORDS[result.id]
    if result.id not in SDK_COLUMNS:
        return None
    column, worst_first = SDK_COLUMNS[result.id]
    rows = result.evidence[result.evidence["status"] == result.status]
    if worst_first:
        rows = rows.sort_values(worst_first, ascending=False)
    name = rows.iloc[0][column] if not rows.empty else None
    # $lib can hold any text a customer sends, so only trust PostHog's own SDK names.
    return name if name in POSTHOG_SDK_NAMES else None


def _is_linked(results, area):
    """Return True if C4 and C5 both failed on a ticket about one of them."""
    statuses = {result.id: result.status for result in results}
    return area in LINKED_AREAS and all(statuses.get(id) == "fail" for id in LINKED_CHECKS)


def _relevant_ids(results, area):
    """Return the ids of the checks that matter for this ticket."""
    linked = LINKED_CHECKS if _is_linked(results, area) else ()
    # C9 and C10 are looked up for this ticket, so they always count.
    return {*AREA_CHECKS.get(area, []), *linked, "C9", "C10"}


def _search_keywords(keywords, results, area):
    """Put the top relevant check's word first, because the GitHub search only uses the first few keywords.

    A check that isn't relevant adds nothing, because its problem isn't what this ticket is about.
    """
    relevant_ids = _relevant_ids(results, area)
    relevant = [result for result in _sort_results(results, area) if result.id in relevant_ids]
    word = _search_word(relevant[0]) if relevant and relevant[0].status in ("fail", "warn") else None
    if word is None:
        return keywords
    return [word] + [keyword for keyword in keywords if keyword != word]


def _link_findings(results, area):
    """Mark which results matter for this ticket. Returns the shared root cause text, or None."""
    relevant_ids = _relevant_ids(results, area)
    for result in results:
        result.relevant = result.id in relevant_ids
    return SHARED_ROOT_CAUSE if _is_linked(results, area) else None


def _table_cell(value):
    """A | would start a new column in a Markdown table, so escape it and keep the text on one line."""
    if isinstance(value, float):
        value = round(value, 1)
    return " ".join(str(value).split()).replace("|", "\\|")


def _evidence_table(evidence):
    """Write the first few rows of a DataFrame as a Markdown table."""
    columns = list(evidence.columns)
    lines = [
        "| " + " | ".join(_table_cell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in evidence.head(MAX_EVIDENCE_ROWS).itertuples(index=False):
        lines.append("| " + " | ".join(_table_cell("" if pd.isna(value) else value) for value in row) + " |")
    if len(evidence) > MAX_EVIDENCE_ROWS:
        lines += ["", f"Showing {MAX_EVIDENCE_ROWS} of {len(evidence)} rows."]
    return lines


def _problems(results):
    """Return the relevant checks to explain, and the relevant checks that couldn't run."""
    relevant = [result for result in results if result.relevant and result.id not in ("C9", "C10")]
    # Explain the failures. If nothing failed, explain the warnings instead.
    problems = [r for r in relevant if r.status == "fail"] or [r for r in relevant if r.status == "warn"]
    # A check that couldn't run hasn't cleared anything, so never report it as "no problem".
    could_not_run = [r for r in relevant if r.status == "error"]
    return problems, could_not_run


def _likely_cause(results, shared_cause):
    """The likely cause, as a list of {"check", "text"}. "check" is None for a line that isn't about one check.

    The note, the API and the Slack bot all show this, so they always agree.
    """
    problems, could_not_run = _problems(results)
    items = [{"check": f"{r.id} {r.name}", "text": r.likely_cause} for r in problems]
    items += [{"check": f"{r.id} {r.name}", "text": COULD_NOT_RUN} for r in could_not_run]
    if shared_cause:
        items.append({"check": None, "text": shared_cause})
    return items or [{"check": None, "text": NO_PROBLEM_FOUND}]


def _build_note(ticket, classification, results, shared_cause, classify_seconds, anchor_seconds):
    """Write the internal note for the person handling the ticket."""
    checks = [result for result in results if result.id not in ("C9", "C10")]
    relevant = [result for result in checks if result.relevant]
    problems, could_not_run = _problems(results)
    by_id = {result.id: result for result in results}
    github, docs = by_id["C9"], by_id["C10"]

    # The API sends id as None when it isn't given, so .get()'s default wouldn't be enough here.
    title = f"{ticket.get('id') or 'Ticket'}: {ticket.get('subject') or 'No subject'}"
    summary = f"**Area:** {classification['area']} | **Team:** {classification['team']} | **Severity:** {classification['severity']}"
    if classification["flag_key"]:
        summary += f" | **Flag:** `{classification['flag_key']}`"
    lines = [f"## {title}", "", summary]
    if classification.get("flag_lookup_failed"):
        lines += ["", f"**{FLAG_LOOKUP_WARNING}**"]

    lines += ["", "### Likely cause", ""]
    for item in _likely_cause(results, shared_cause):
        if item["check"]:
            lines.append(f"- **{item['check']}:** {item['text']}")
        elif item["text"] == NO_PROBLEM_FOUND:
            lines.append(NO_PROBLEM_FOUND)
        else:
            lines.append(f"- **{item['text']}**")

    lines += ["", "### Evidence", ""]
    if not relevant:
        lines.append("No checks belong to this ticket's area.")
    for result in relevant:
        lines += [f"**{result.id} {result.name} ({result.status}):** {result.headline}", ""]
        if not result.evidence.empty:
            lines += _evidence_table(result.evidence) + [""]
    # A check that couldn't run might have found something, so never hide it.
    for result in checks:
        if result.status == "error" and not result.relevant:
            lines += [f"**{result.id} {result.name} (error):** {result.headline}", ""]
    if lines[-1] == "":
        lines.pop()

    lines += ["", "### Next steps", ""]
    next_steps = list(dict.fromkeys(r.next_step for r in problems + could_not_run))
    lines += [f"{number}. {step}" for number, step in enumerate(next_steps, 1)] or ["No action needed from these checks."]

    lines += ["", "### Also found", ""]
    also_found = [r for r in checks if r.status == "fail" and not r.relevant]
    lines += [f"- **{r.id} {r.name}:** {r.headline}" for r in also_found] or ["Nothing else failed."]

    lines += ["", "### Related issues", "", github.headline]
    if github.search_terms:
        lines += ["", f"Searched GitHub for: {', '.join(github.search_terms)}"]
    if not github.evidence.empty:
        lines.append("")
        lines += [f"- [#{row.number} {row.title}]({row.url}) ({row.state})" for row in github.evidence.itertuples()]

    slowest = max(results, key=lambda result: result.seconds)
    lines += ["", "### Docs", "", docs.docs_url or docs.headline, "", "---", "", FOOTER]
    lines += ["", (
        f"Slowest check: {slowest.id} {slowest.name}, {slowest.seconds:.2f} seconds. "
        f"Classify: {classify_seconds:.2f} seconds. Anchor lookup: {anchor_seconds:.2f} seconds."
    )]
    return "\n".join(lines)


def diagnose(ticket: dict, source: str = "app") -> dict:
    """Classify a ticket, run all the checks, and write up what they found.

    `source` says where the diagnosis was run from ("app" or "slack"). It's only used for telemetry.
    """
    started = time.perf_counter()

    # Step 1: classify the ticket. A fresh lookup means new flag keys are known too.
    _fetch_flag_keys.cache_clear()
    classification = classify(ticket)
    classify_seconds = time.perf_counter() - started
    area = classification["area"]

    # Step 2: run C1 to C10. C1 to C8 run together, and C9 then uses what they found.
    anchor_seconds = _lookup_anchor()
    results = _run_checks(classification["flag_key"])
    keywords = _search_keywords(classification["keywords"], results, area)
    results.append(_run_check(check_c9_github, keywords, area))
    results.append(_run_check(check_c10_docs, area))

    # Step 3: sort the results.
    results = _sort_results(results, area)

    # Step 4: link findings and mark what's relevant.
    shared_cause = _link_findings(results, area)

    # Step 5: write the note.
    diagnosis = {
        "ticket": ticket,
        "classification": classification,
        "results": results,
        "note_markdown": _build_note(ticket, classification, results, shared_cause, classify_seconds, anchor_seconds),
        "seconds": round(time.perf_counter() - started, 2),
    }

    # Step 6: record the run in our internal PostHog project. It never raises, so it can't break the diagnosis.
    log_diagnosis(diagnosis, source)
    return diagnosis


def _plain_value(value):
    """Turn a table value into something JSON can hold. Blanks and infinity become None."""
    if pd.isna(value):
        return None
    # NumPy numbers have .item(), which gives the normal Python number.
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_json(diagnosis):
    """Return a copy of a diagnosis that the API can send as JSON, with each DataFrame as a list of rows.

    It also adds "likely_cause", the same lines as the note's Likely cause section, so callers don't need the rules.
    """
    area = diagnosis["classification"]["area"]
    shared_cause = SHARED_ROOT_CAUSE if _is_linked(diagnosis["results"], area) else None
    likely_cause = _likely_cause(diagnosis["results"], shared_cause)
    results = []
    for result in diagnosis["results"]:
        rows = [
            {column: _plain_value(value) for column, value in row.items()}
            for row in result.evidence.to_dict("records")
        ]
        results.append({**vars(result), "evidence": rows})
    return {**diagnosis, "results": results, "likely_cause": likely_cause}
