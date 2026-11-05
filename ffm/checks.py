"""Health checks that look for common problems in a PostHog project."""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pandas as pd
import requests

from ffm.config import ANCHOR_MODE, CUSTOMER_PROJECT_ID
from ffm.posthog_api import get_feature_flags, run_hogql

# Finds the newest event, which time windows are measured from in latest_event mode.
ANCHOR_QUERY = "SELECT max(timestamp) FROM events WHERE event NOT LIKE 'ffm_%'"

# Queries come from queries.md. The only addition is the ffm_ line, which leaves out our own tool's events.
# Their now() is swapped for the anchor when they run, so always run them through _anchored().
Q1 = """SELECT properties.$lib AS lib,
       max(timestamp) AS last_seen,
       dateDiff('hour', max(timestamp), now()) AS hours_silent
FROM events
WHERE timestamp > now() - INTERVAL 14 DAY
  AND properties.$lib IS NOT NULL
  AND event NOT LIKE 'ffm_%'
GROUP BY lib
ORDER BY hours_silent DESC"""

Q2 = """SELECT properties.$lib AS lib,
       properties.$lib_version AS version,
       count() AS events
FROM events
WHERE timestamp > now() - INTERVAL 7 DAY
  AND properties.$lib IS NOT NULL
  AND event NOT LIKE 'ffm_%'
GROUP BY lib, version
ORDER BY lib, events DESC"""

Q3 = """SELECT properties.$lib_version AS version,
       count() AS pageviews,
       countIf(properties.$current_url IS NULL
               OR properties.$current_url = '') AS no_url
FROM events
WHERE event = '$pageview'
  AND timestamp > now() - INTERVAL 7 DAY
  AND event NOT LIKE 'ffm_%'
GROUP BY version"""

Q4 = """SELECT properties.$lib AS lib,
       count(DISTINCT distinct_id) AS distinct_ids,
       countIf(event = '$identify') AS identify_calls
FROM events
WHERE timestamp > now() - INTERVAL 14 DAY
  AND event NOT LIKE 'ffm_%'
GROUP BY lib
ORDER BY distinct_ids DESC"""

# Q5's persons query, with the blanks filled in from each flag's own condition.
# It reads the persons table, not events, so it has no time window and no ffm_ line.
Q5_PERSONS = """SELECT count() AS persons,
       {with_columns}countIf({condition}) AS matching_flag
FROM persons"""

Q5_CALLS = """SELECT properties.$feature_flag AS flag,
       properties.$feature_flag_response AS response,
       count() AS calls
FROM events
WHERE event = '$feature_flag_called'
  AND timestamp > now() - INTERVAL 7 DAY
  AND event NOT LIKE 'ffm_%'
GROUP BY flag, response"""

Q6 = """SELECT JSONExtractString(properties.$exception_list, 1, 'type') AS type,
       JSONExtractString(properties.$exception_list, 1, 'value') AS message,
       properties.app_version AS app_version,
       count() AS last_24h
FROM events
WHERE event = '$exception'
  AND timestamp > now() - INTERVAL 1 DAY
  AND event NOT LIKE 'ffm_%'
GROUP BY type, message, app_version
ORDER BY last_24h DESC
LIMIT 5"""

# Q6 only lists the top 5 errors, so the totals for C6's comparison come from here.
Q6_TOTALS = """SELECT countIf(timestamp > now() - INTERVAL 1 DAY) AS last_24h,
       countIf(timestamp <= now() - INTERVAL 1 DAY) / 13 AS daily_average
FROM events
WHERE event = '$exception'
  AND timestamp > now() - INTERVAL 14 DAY
  AND event NOT LIKE 'ffm_%'"""

Q7 = """SELECT event,
       count() AS duplicate_groups
FROM (
  SELECT event, distinct_id, timestamp, count() AS copies
  FROM events
  WHERE timestamp > now() - INTERVAL 7 DAY
    AND event NOT LIKE 'ffm_%'
  GROUP BY event, distinct_id, timestamp
  HAVING copies > 1
)
GROUP BY event"""

# Q7 counts groups, but C7's rule is about rows, so this counts each event's rows and spare copies.
Q7_ROWS = """SELECT event,
       sum(copies) AS total_rows,
       sum(copies - 1) AS duplicate_rows
FROM (
  SELECT event, distinct_id, timestamp, count() AS copies
  FROM events
  WHERE timestamp > now() - INTERVAL 7 DAY
    AND event NOT LIKE 'ffm_%'
  GROUP BY event, distinct_id, timestamp
)
GROUP BY event
ORDER BY duplicate_rows DESC"""

# C8's query. The 7-day average includes the last 24 hours.
Q8 = """SELECT countIf(timestamp > now() - INTERVAL 1 DAY) AS last_24h,
       count() / 7 AS daily_average
FROM events
WHERE timestamp > now() - INTERVAL 7 DAY
  AND event NOT LIKE 'ffm_%'"""

# Where to find each SDK's latest version. Note that posthog-python's package is just called "posthog".
LATEST_VERSION_URLS = {
    "posthog-js": "https://registry.npmjs.org/posthog-js/latest",
    "posthog-react-native": "https://registry.npmjs.org/posthog-react-native/latest",
    "posthog-python": "https://pypi.org/pypi/posthog/json",
}

# Stop waiting for npm or PyPI after this many seconds.
REGISTRY_TIMEOUT_SECONDS = 10

# Failures we turn into an "error" result instead of crashing: PostHog refusals and network problems.
EXPECTED_ERRORS = (RuntimeError, requests.RequestException)


@dataclass
class CheckResult:
    """The outcome of one check."""

    id: str
    name: str
    status: str  # one of: pass, warn, fail, info, error
    headline: str  # one plain-English sentence with the actual numbers
    evidence: pd.DataFrame  # the table behind the headline
    likely_cause: str
    next_step: str
    docs_url: str
    query: str  # the HogQL that was run
    relevant: bool = False  # does this matter for the ticket? diagnose() fills this in
    seconds: float = 0.0  # how long the check took. diagnose() fills this in
    retried: bool = False  # did PostHog need a second try? diagnose() fills this in
    search_terms: list = field(default_factory=list)  # the exact terms sent to GitHub. Only C9 uses this


@lru_cache(maxsize=None)
def get_anchor(project_id=CUSTOMER_PROJECT_ID):
    """Return the newest event's timestamp, or None in now mode or if there are no events.

    The answer is remembered, so all checks in a run share one anchor. Call get_anchor.cache_clear() to refresh it.
    """
    if ANCHOR_MODE == "now":
        return None
    result = run_hogql(ANCHOR_QUERY, project_id)
    anchor = result.iloc[0, 0] if not result.empty else None
    # With no events, PostHog may answer with a blank or with the earliest possible date, 1970-01-01.
    if pd.isna(anchor) or str(anchor).startswith("1970-01-01"):
        return None
    return anchor


def _anchored(query, project_id):
    """Swap now() in a query for the anchor, if there is one."""
    anchor = get_anchor(project_id)
    return query if anchor is None else query.replace("now()", f"toDateTime('{anchor}')")


def _worst_status(statuses):
    """Return the most serious status in a list."""
    for status in ("fail", "warn"):
        if status in statuses:
            return status
    return "pass"


def _hours(count):
    """Write a number of hours in plain English, like "1 hour" or "72 hours"."""
    return f"{count:,} hour" if count == 1 else f"{count:,} hours"


def _error_result(id, name, query, docs_url, error):
    """Build the result to return when a check couldn't run at all."""
    return CheckResult(
        id, name, "error", f"{name} could not run: {error}", pd.DataFrame(),
        "The check failed before it could look at the data.",
        "Fix the error above and run the check again.",
        docs_url, query,
    )


def _no_data_result(id, name, query, docs_url, headline):
    """Build the result to return when the query found no events to judge."""
    return CheckResult(
        id, name, "info", headline, pd.DataFrame(),
        "No matching events in the time window.",
        "Check that this is the right project and that events are arriving.",
        docs_url, query,
    )


def check_c1_sdk_quiet(project_id=CUSTOMER_PROJECT_ID):
    """C1: has one SDK gone quiet compared with the most recently active SDK?"""
    id, name, docs_url = "C1", "SDK went quiet", "https://posthog.com/docs/libraries"
    query = Q1
    try:
        query = _anchored(Q1, project_id)
        evidence = run_hogql(query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)
    if evidence.empty:
        return _no_data_result(id, name, query, docs_url, "No SDK has sent events in the last 14 days")

    # The rule compares SDKs with each other, so work out how far each one is behind the newest.
    newest = evidence.loc[evidence["hours_silent"].idxmin()]
    evidence["hours_behind_newest"] = evidence["hours_silent"] - newest["hours_silent"]

    def status_for(gap):
        if gap >= 24:
            return "fail"
        return "warn" if gap >= 6 else "pass"

    evidence["status"] = evidence["hours_behind_newest"].map(status_for)
    status = _worst_status(list(evidence["status"]))

    # The query sorts the quietest SDK first.
    quietest = evidence.iloc[0]
    gap = int(quietest["hours_behind_newest"])
    if status == "pass":
        return CheckResult(
            id, name, status,
            f"All {len(evidence)} SDKs are active, with at most {_hours(gap)} between them",
            evidence, "", "No action needed.", docs_url, query,
        )
    return CheckResult(
        id, name, status,
        f"{quietest['lib']} has sent nothing for {_hours(int(quietest['hours_silent']))}, " + (
            # When the newest SDK has no silence of its own, "73 hours, 73 hours longer" would just repeat itself.
            f"while {newest['lib']} kept sending" if newest["hours_silent"] == 0
            else f"{_hours(gap)} longer than {newest['lib']}"
        ),
        evidence,
        f"{quietest['lib']} stopped while other SDKs kept sending, which points to that app or service "
        "rather than the whole project. A recent release may have removed or broken the SDK.",
        f"Check what changed in the {quietest['lib']} app around {quietest['last_seen']}, "
        "starting with its latest release and SDK setup.",
        docs_url, query,
    )


@lru_cache(maxsize=None)
def get_latest_version(lib):
    """Ask npm or PyPI for an SDK's latest version. The answer is remembered, so each SDK is fetched once."""
    response = requests.get(LATEST_VERSION_URLS[lib], timeout=REGISTRY_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    # npm has the version at the top of its reply, and PyPI puts it under "info".
    return data["info"]["version"] if "info" in data else data["version"]


def _major_minor(version):
    """Turn "1.36.0" into (1, 36), or return None if it doesn't look like a version number."""
    match = re.match(r"(\d+)\.(\d+)", str(version))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _compare_version(version, latest):
    """Return (minor releases behind, status). The number is None if minor numbers can't be compared."""
    used, newest = _major_minor(version), _major_minor(latest)
    if used is None or newest is None:
        return None, "info"
    # Minor numbers restart at each major version, so subtracting them would mean nothing.
    if used[0] < newest[0]:
        return None, "warn"
    # max() stops a version newer than the registry's latest from counting as negative.
    behind = max(newest[1] - used[1], 0) if used[0] == newest[0] else 0
    if behind >= 50:
        return behind, "fail"
    return behind, "warn" if behind >= 10 else "pass"


def check_c2_sdk_versions(project_id=CUSTOMER_PROJECT_ID):
    """C2: is each SDK version in use close to the latest release?"""
    id, name, docs_url = "C2", "SDK version freshness", "https://posthog.com/docs/libraries"
    query = Q2
    try:
        query = _anchored(Q2, project_id)
        evidence = run_hogql(query, project_id)
        if evidence.empty:
            return _no_data_result(id, name, query, docs_url, "No SDK has sent events in the last 7 days")
        # SDKs we have no registry address for are shown in the table but not judged.
        evidence["latest"] = [
            get_latest_version(lib) if lib in LATEST_VERSION_URLS else None for lib in evidence["lib"]
        ]
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)

    comparisons = [_compare_version(v, l) for v, l in zip(evidence["version"], evidence["latest"])]
    # Keep this column as whole numbers, with a blank where minor numbers can't be compared.
    evidence["minors_behind"] = pd.array([c[0] for c in comparisons], dtype="Int64")
    evidence["status"] = [c[1] for c in comparisons]
    status = _worst_status(list(evidence["status"]))

    behind = evidence[evidence["status"].isin(["fail", "warn"])]
    if status == "pass":
        return CheckResult(
            id, name, status,
            f"All {len(evidence)} SDK versions in use are fewer than 10 minor releases behind the latest",
            evidence, "", "No action needed.", docs_url, query,
        )

    # Lead with the most serious version, then the one furthest behind.
    worst = behind[behind["status"] == status].sort_values("minors_behind", ascending=False).iloc[0]
    if pd.isna(worst["minors_behind"]):
        how_far = f"on an older major version than the latest {worst['latest']}"
    else:
        how_far = f"{int(worst['minors_behind']):,} minor releases behind the latest {worst['latest']}"
    headline = f"{worst['lib']} {worst['version']} is {how_far}, and sent {int(worst['events']):,} events in 7 days"
    if len(behind) > 1:
        headline += f" ({len(behind) - 1} more also out of date)"
    return CheckResult(
        id, name, status, headline, evidence,
        f"Part of the traffic is still on {worst['lib']} {worst['version']}, "
        "often from an old site or a pinned version that was never upgraded.",
        f"Find where {worst['lib']} {worst['version']} is installed and upgrade it to {worst['latest']}.",
        docs_url, query,
    )


def check_c3_missing_properties(project_id=CUSTOMER_PROJECT_ID):
    """C3: how many pageviews have no $current_url?"""
    id, name, docs_url = "C3", "Missing core properties", "https://posthog.com/docs/data/events"
    query = Q3
    try:
        query = _anchored(Q3, project_id)
        evidence = run_hogql(query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)
    if evidence.empty:
        return _no_data_result(id, name, query, docs_url, "No $pageview events in the last 7 days")

    evidence["pct_no_url"] = (100 * evidence["no_url"] / evidence["pageviews"]).round(1)
    total = int(evidence["pageviews"].sum())
    missing = int(evidence["no_url"].sum())
    percent = 100 * missing / total

    if percent > 10:
        status = "fail"
    elif percent >= 1:
        status = "warn"
    else:
        status = "pass"

    summary = f"{missing:,} of {total:,} pageviews ({percent:.1f}%) in 7 days have no $current_url"
    if status == "pass":
        return CheckResult(id, name, status, summary, evidence, "", "No action needed.", docs_url, query)

    # Name the versions responsible, since blank URLs usually come from one place.
    versions = evidence[evidence["no_url"] > 0].sort_values("no_url", ascending=False)["version"].astype(str)
    version_list = ", ".join(versions)
    return CheckResult(
        id, name, status, f"{summary}, from SDK version {version_list}", evidence,
        f"The blank URLs come from SDK version {version_list}. An old SDK or a custom capture call "
        "may be sending pageviews without the URL.",
        f"Find where version {version_list} is installed, upgrade it, and check its pageviews include $current_url.",
        docs_url, query,
    )


def check_c4_identification(project_id=CUSTOMER_PROJECT_ID):
    """C4: is each SDK identifying its users?"""
    id, name, docs_url = "C4", "Identification health", "https://posthog.com/docs/product-analytics/identify"
    query = Q4
    try:
        query = _anchored(Q4, project_id)
        evidence = run_hogql(query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)
    if evidence.empty:
        return _no_data_result(id, name, query, docs_url, "No events in the last 14 days")

    evidence["identify_pct"] = (100 * evidence["identify_calls"] / evidence["distinct_ids"]).round(1)

    def status_for(row):
        if row["distinct_ids"] > 500 and row["identify_calls"] == 0:
            return "fail"
        # Compare the raw numbers, because the rounded percentage could hide a value just under 1%.
        return "warn" if row["identify_calls"] < 0.01 * row["distinct_ids"] else "pass"

    evidence["status"] = evidence.apply(status_for, axis=1)
    status = _worst_status(list(evidence["status"]))

    if status == "pass":
        return CheckResult(
            id, name, status,
            f"All {len(evidence)} SDKs have $identify calls for at least 1% of their distinct IDs",
            evidence, "", "No action needed.", docs_url, query,
        )

    # The query sorts the most distinct IDs first, so this picks the largest SDK with the worst status.
    flagged = evidence[evidence["status"].isin(["fail", "warn"])]
    worst = flagged[flagged["status"] == status].iloc[0]
    lib = worst["lib"] if pd.notna(worst["lib"]) else "Events with no $lib"
    headline = (
        f"{lib} has {int(worst['distinct_ids']):,} distinct IDs and "
        f"{int(worst['identify_calls']):,} $identify calls in 14 days"
    )
    others = len(flagged) - 1
    if others:
        headline += f" ({others} more {'SDK' if others == 1 else 'SDKs'} also flagged)"
    return CheckResult(
        id, name, status, headline, evidence,
        "Without identify calls, each anonymous ID counts as a separate person. This inflates unique "
        "users, and targeting that relies on person properties may not match them.",
        f"Check whether {lib} calls posthog.identify() after login or signup.",
        docs_url, query,
    )


def _sql_text(value):
    """Make a value safe to put inside a quoted HogQL string."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _person_field(key):
    """Write a person property for HogQL. Unusual names, like ones with spaces, need backticks."""
    if re.fullmatch(r"\$?\w+", key):
        return f"properties.{key}"
    escaped = key.replace("\\", "\\\\").replace("`", "\\`")
    return f"properties.`{escaped}`"


def _person_condition(prop):
    """Turn one flag condition into HogQL, or return None if this check can't test it."""
    if prop.get("type") != "person":
        return None  # for example, a cohort
    field = _person_field(prop["key"])
    operator = prop.get("operator") or "exact"  # PostHog treats a missing operator as exact
    value = prop.get("value")
    if operator == "icontains":
        # In ILIKE, % and _ are wildcards, so a backslash has to make them literal first.
        literal = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"{field} ILIKE '%{_sql_text(literal)}%'"
    if operator == "exact":
        values = value if isinstance(value, list) else [value]
        # PostHog's exact match ignores upper and lower case, so compare everything in lower case.
        quoted = [f"'{_sql_text(str(v).lower())}'" for v in values]
        return f"lower({field}) IN ({', '.join(quoted)})"
    if operator == "is_set":
        return f"{field} IS NOT NULL"
    return None


def _flag_match(flag):
    """Work out who a flag can match. Returns (HogQL condition, property names).

    The condition is None if we can't test the flag, and "" if it has no conditions, so anyone can match.
    """
    filters = flag.get("filters") or {}
    # A flag aimed at companies or other groups doesn't match on persons.
    if filters.get("aggregation_group_type_index") is not None:
        return None, []

    group_conditions = []
    keys = []
    for group in filters.get("groups") or []:
        props = group.get("properties") or []
        if not props:
            return "", []
        conditions = [_person_condition(prop) for prop in props]
        if None in conditions:
            return None, []
        # All conditions in a group must match, but any one group is enough.
        group_conditions.append(" AND ".join(conditions))
        keys += [prop["key"] for prop in props]
    if not group_conditions:
        return "", []
    if len(group_conditions) == 1:
        return group_conditions[0], list(dict.fromkeys(keys))
    return " OR ".join(f"({c})" for c in group_conditions), list(dict.fromkeys(keys))


def _describe_condition(flag):
    """Write a flag's conditions in a short readable form, like "email icontains @tidewater.io"."""
    parts = []
    for group in (flag.get("filters") or {}).get("groups") or []:
        for prop in group.get("properties") or []:
            parts.append(f"{prop.get('key')} {prop.get('operator') or 'exact'} {prop.get('value')}".strip())
    return "; ".join(parts) or "no conditions"


def _check_one_flag(flag, calls, project_id):
    """Judge one flag. Returns (a row for the evidence table, the persons query, or None if none was run)."""
    flag_calls = calls[calls["flag"] == flag["key"]]
    is_false = flag_calls["response"].astype(str).str.lower() == "false"
    row = {
        "flag": flag["key"],
        "active": flag.get("active"),
        "condition": _describe_condition(flag),
        "calls_7d": int(flag_calls["calls"].sum()),
        "false_calls_7d": int(flag_calls.loc[is_false, "calls"].sum()),
    }

    condition, keys = _flag_match(flag)
    query = None
    if condition:
        with_columns = "".join(
            f"countIf({_person_field(key)} IS NOT NULL) AS with_{re.sub(r'[^0-9A-Za-z_]', '_', key)},\n       "
            for key in keys
        )
        query = Q5_PERSONS.format(with_columns=with_columns, condition=condition)
        # Pandas numbers don't always print cleanly, so turn each count into a plain int.
        row.update({k: int(v) for k, v in run_hogql(_anchored(query, project_id), project_id).iloc[0].items()})

    if not row["active"] or row.get("matching_flag") == 0:
        row["status"] = "fail"
    elif row["calls_7d"] > 0 and row["false_calls_7d"] == row["calls_7d"]:
        row["status"] = "warn"
    else:
        row["status"] = "info" if condition is None else "pass"
    return row, query


def check_c5_flags(flag_key=None, project_id=CUSTOMER_PROJECT_ID):
    """C5: can each feature flag, or just flag_key if given, match anyone?"""
    id, name, docs_url = "C5", "Feature flag can match", "https://posthog.com/docs/feature-flags"
    query = Q5_CALLS
    try:
        query = _anchored(Q5_CALLS, project_id)
        all_flags = get_feature_flags(project_id)
        flags = [flag for flag in all_flags if flag["key"] == flag_key] if flag_key else all_flags
        if not flags:
            known = pd.DataFrame({"flag": [flag["key"] for flag in all_flags]})
            headline = f"No feature flag called {flag_key} in this project" if flag_key else "This project has no feature flags"
            return CheckResult(
                id, name, "info", headline, known, "",
                "Check the flag's key and that this is the right project.", docs_url, query,
            )
        calls = run_hogql(query, project_id)
        results = [_check_one_flag(flag, calls, project_id) for flag in flags]
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)

    rows = [row for row, _ in results]
    query = "\n\n".join([person_query for _, person_query in results if person_query] + [query])
    evidence = pd.DataFrame(rows)
    statuses = list(evidence["status"])
    if all(s == "info" for s in statuses):
        return CheckResult(
            id, name, "info",
            f"Can't test the conditions of {len(rows)} flag{'' if len(rows) == 1 else 's'}, "
            "because they use cohorts, groups or a less common operator",
            evidence, "", "Check these flags' conditions in the PostHog app.", docs_url, query,
        )
    status = _worst_status(statuses)
    if status == "pass":
        tested = statuses.count("pass")
        return CheckResult(
            id, name, status, f"{tested} of {len(rows)} flags tested are active and can match someone",
            evidence, "", "No action needed.", docs_url, query,
        )

    worst = next(row for row in rows if row["status"] == status)
    flag, calls_7d = worst["flag"], worst["calls_7d"]
    # Each with_ column counts the persons who have one property the flag looks at.
    have = [
        f"{column[5:]} is set on {worst[column]:,} of {worst['persons']:,} persons"
        for column in worst if column.startswith("with_")
    ]
    if not worst["active"]:
        headline = f"{flag} is turned off, so it returns false for everyone"
        cause = "The flag is switched off."
        next_step = f"Turn on {flag} if it should be live."
    elif status == "fail":
        headline = f"{flag} is active, but 0 of {worst['persons']:,} persons match {worst['condition']}"
        if calls_7d > 0 and worst["false_calls_7d"] == calls_7d:
            headline += f", and all {calls_7d:,} $feature_flag_called events in 7 days returned false"
        cause = (
            f"{' and '.join(have)}, and nobody matches. The flag reads person properties, "
            "so it can't match users whose properties were never set."
        )
        next_step = "Check that posthog.identify() sets these properties on your users, and that the condition's value is right."
    else:
        headline = f"All {calls_7d:,} $feature_flag_called events for {flag} in 7 days returned false"
        cause = "Some people could match the flag, but none of those calling it did."
        next_step = f"Compare the users calling {flag} with its condition ({worst['condition']}) and rollout."
    others = sum(s in ("fail", "warn") for s in statuses) - 1
    if others:
        headline += f" ({others} more {'flag' if others == 1 else 'flags'} also flagged)"
    return CheckResult(id, name, status, headline, evidence, cause, next_step, docs_url, query)


def check_c6_error_spike(project_id=CUSTOMER_PROJECT_ID):
    """C6: are the last 24 hours' errors far above the daily average of the 13 days before?"""
    id, name, docs_url = "C6", "Error spike", "https://posthog.com/docs/error-tracking"
    query = f"{Q6}\n\n{Q6_TOTALS}"
    try:
        top_errors_query = _anchored(Q6, project_id)
        totals_query = _anchored(Q6_TOTALS, project_id)
        query = f"{top_errors_query}\n\n{totals_query}"
        evidence = run_hogql(top_errors_query, project_id)
        totals = run_hogql(totals_query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)

    last_24h = int(totals.iloc[0]["last_24h"]) if not totals.empty else 0
    average = float(totals.iloc[0]["daily_average"]) if not totals.empty else 0
    if last_24h == 0 and average == 0:
        return _no_data_result(id, name, query, docs_url, "No $exception events in the last 14 days")

    # With no errors in the earlier days, any error at all is an infinitely large jump.
    if last_24h == 0:
        ratio = 0
    else:
        ratio = last_24h / average if average else float("inf")
    if ratio > 5 and last_24h > 50:
        status = "fail"
    elif ratio >= 2:
        status = "warn"  # this also covers over 5x with 50 errors or fewer, which is too small to fail
    else:
        status = "pass"

    comparison = "with none in the 13 days before" if not average else (
        f"{ratio:.1f}x the daily average of {average:.1f} from the 13 days before"
    )
    summary = f"{last_24h:,} errors in the last 24 hours, {comparison}"
    if status == "pass":
        return CheckResult(id, name, status, summary, evidence, "", "No action needed.", docs_url, query)

    top = evidence.iloc[0]
    top_count = int(top["last_24h"])
    version = f"app_version {top['app_version']}" if pd.notna(top["app_version"]) else "no app_version"
    what = f"{top['type']}: {top['message']}"
    # One error making up nearly all of the spike points to one change in the customer's code.
    if top_count >= 0.8 * last_24h:
        cause = f"{top_count:,} of the {last_24h:,} errors are one error on {version}, which points to a change in that release rather than PostHog."
    else:
        cause = f"The most common error accounts for {top_count:,} of the {last_24h:,}, so several things may have changed."
    return CheckResult(
        id, name, status, f'{summary}. Most common: "{what}" on {version} ({top_count:,})', evidence, cause,
        f"Look at what changed in {version}, starting with where this error is thrown.",
        docs_url, query,
    )


def check_c7_duplicates(project_id=CUSTOMER_PROJECT_ID):
    """C7: are any events stored more than once with the same event, person and timestamp?"""
    id, name, docs_url = "C7", "Duplicate events", "https://posthog.com/docs/data/events"
    query = f"{Q7}\n\n{Q7_ROWS}"
    try:
        groups_query = _anchored(Q7, project_id)
        rows_query = _anchored(Q7_ROWS, project_id)
        query = f"{groups_query}\n\n{rows_query}"
        groups = run_hogql(groups_query, project_id)
        evidence = run_hogql(rows_query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)
    if evidence.empty:
        return _no_data_result(id, name, query, docs_url, "No events in the last 7 days")

    # Add Q7's group counts. Events with no duplicates aren't in Q7, so they get 0.
    evidence = evidence.merge(groups, on="event", how="left")
    evidence["duplicate_groups"] = evidence["duplicate_groups"].fillna(0).astype(int)
    evidence["pct_duplicate"] = (100 * evidence["duplicate_rows"] / evidence["total_rows"]).round(1)

    def status_for(row):
        # Compare the raw numbers, because the rounded percentage could hide a value just over a limit.
        if row["duplicate_rows"] > 0.10 * row["total_rows"]:
            return "fail"
        return "warn" if row["duplicate_rows"] >= 0.01 * row["total_rows"] else "pass"

    evidence["status"] = evidence.apply(status_for, axis=1)
    status = _worst_status(list(evidence["status"]))
    if status == "pass":
        total = int(evidence["total_rows"].sum())
        spare = int(evidence["duplicate_rows"].sum())
        return CheckResult(
            id, name, status,
            f"{spare:,} of {total:,} event rows in 7 days are exact duplicates, under 1% for every event",
            evidence, "", "No action needed.", docs_url, query,
        )

    flagged = evidence[evidence["status"].isin(["fail", "warn"])]
    worst = flagged[flagged["status"] == status].sort_values("duplicate_rows", ascending=False).iloc[0]
    event = worst["event"]
    percent = 100 * worst["duplicate_rows"] / worst["total_rows"]
    headline = (
        f"{int(worst['duplicate_rows']):,} of {int(worst['total_rows']):,} {event} rows ({percent:.1f}%) "
        "in 7 days are exact duplicates"
    )
    others = len(flagged) - 1
    if others:
        headline += f" ({others} more {'event' if others == 1 else 'events'} also flagged)"
    return CheckResult(
        id, name, status, headline, evidence,
        f"The same {event} is being sent more than once, often from a retry or a second capture call. "
        "Any count or sum built on it is too high.",
        f"Find where {event} is captured and stop the repeat. Sending a fixed uuid with each event also lets PostHog drop repeats.",
        docs_url, query,
    )


def check_c8_volume_drop(project_id=CUSTOMER_PROJECT_ID):
    """C8: did the last 24 hours bring far fewer events than the 7-day daily average?"""
    id, name, docs_url = "C8", "Overall volume drop", "https://posthog.com/docs/data/events"
    query = Q8
    try:
        query = _anchored(Q8, project_id)
        evidence = run_hogql(query, project_id)
    except EXPECTED_ERRORS as error:
        return _error_result(id, name, query, docs_url, error)
    if evidence.empty or not evidence.iloc[0]["daily_average"]:
        return _no_data_result(id, name, query, docs_url, "No events in the last 7 days")

    last_24h = int(evidence.iloc[0]["last_24h"])
    average = float(evidence.iloc[0]["daily_average"])
    percent = 100 * last_24h / average
    evidence["pct_of_average"] = round(percent, 1)

    if percent < 25:
        status = "fail"
    elif percent <= 50:
        status = "warn"
    else:
        status = "pass"

    headline = f"{last_24h:,} events in the last 24 hours, {percent:.0f}% of the 7-day daily average of {average:,.0f}"
    if status == "pass":
        return CheckResult(id, name, status, headline, evidence, "", "No action needed.", docs_url, query)
    return CheckResult(
        id, name, status, headline, evidence,
        "Events dropped across the whole project, so this could be a tracking change, an outage or a quiet day.",
        "Run C1 to see whether one SDK stopped, and check for recent releases or changes to tracking.",
        docs_url, query,
    )


# Every check, in order. Each one can be called with no arguments.
ALL_CHECKS = [
    check_c1_sdk_quiet,
    check_c2_sdk_versions,
    check_c3_missing_properties,
    check_c4_identification,
    check_c5_flags,
    check_c6_error_spike,
    check_c7_duplicates,
    check_c8_volume_drop,
]
