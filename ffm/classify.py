"""Sort a support ticket into a product area, team and severity using keyword rules."""
import re
from functools import lru_cache

import requests

from ffm.posthog_api import get_feature_flags

# The words that point to each area. Keep them lowercase. The feature_flags area also counts existing flag keys.
AREA_KEYWORDS = {
    "feature_flags": ["flag", "feature flag", "rollout", "variant"],
    "error_tracking": ["error", "exception", "crash", "stack trace", "spike"],
    "identification": ["unique users", "identify", "anonymous", "person", "persons", "users"],
    "data_quality": ["url", "property", "properties", "blank", "filter"],
    "ingestion": ["missing", "gone", "flat", "no data", "stopped", "not showing", "disappeared"],
}

# Which team handles each area.
AREA_TEAMS = {
    "feature_flags": "Feature Flags",
    "error_tracking": "Error Tracking",
    "identification": "Product Analytics",
    "data_quality": "Client Libraries (SDKs)",
    "ingestion": "Ingestion",
    "general": "Support triage",
}

# When areas tie, the one earliest in this list wins.
TIE_PRIORITY = ["feature_flags", "error_tracking", "identification", "data_quality", "ingestion"]

# Any of these words adds 1 to the severity, however many of them appear.
URGENT_WORDS = ["nobody", "anyone", "none of us", "all", "gone", "spike", "lit up", "down", "broken"]

# What each severity score means.
SEVERITY_LABELS = {0: "low", 1: "medium", 2: "high"}

# The most keywords to return for the GitHub search.
MAX_KEYWORDS = 4

# Failures that shouldn't stop a ticket being classified: PostHog refusals, network problems and odd replies.
FLAG_LOOKUP_ERRORS = (RuntimeError, requests.RequestException, KeyError, TypeError, ValueError)


def _pattern(term, allow_plural=False):
    """Build a search for a whole word or phrase. This stops "all" matching "call"."""
    plural = "(?:e?s)?" if allow_plural else ""
    return rf"(?<![\w-]){re.escape(term)}{plural}(?![\w-])"


def _find(term, text, allow_plural=False):
    """Return where a whole word or phrase is in the text, or None."""
    match = re.search(_pattern(term, allow_plural), text)
    return match.start() if match else None


@lru_cache(maxsize=None)
def _fetch_flag_keys():
    """Ask PostHog for the flag keys. The answer is remembered, so a run fetches them once.

    A failed fetch isn't remembered, so the next ticket tries again. Call _fetch_flag_keys.cache_clear() to refresh.
    """
    return tuple(flag["key"] for flag in get_feature_flags())


def _known_flag_keys():
    """Return all of the project's flag keys, or None if the lookup failed."""
    try:
        return _fetch_flag_keys()
    except FLAG_LOOKUP_ERRORS:
        return None


def _flag_keys_in(text, known_keys):
    """Return the flag keys mentioned in the text, earliest first."""
    found = [(_find(key.lower(), text), key) for key in known_keys or ()]
    return [key for position, key in sorted((f for f in found if f[0] is not None), key=lambda f: f[0])]


def _search_keywords(area_words, known_keys):
    """Choose the keywords for the public GitHub search: only fixed words from AREA_KEYWORDS, never the customer's."""
    secret = [key.lower() for key in known_keys or ()]
    return [word for word in area_words if word not in secret][:MAX_KEYWORDS]


def classify(ticket: dict) -> dict:
    """Return a ticket's area, team, severity, flag_key and keywords."""
    subject = ticket.get("subject") or ""
    # The line break stops a phrase like "no data" from matching across the subject and body.
    text = f"{subject}\n{ticket.get('body') or ''}".lower()

    known_keys = _known_flag_keys()
    flag_keys = _flag_keys_in(text, known_keys)

    # List the keywords found in each area.
    matches = {
        area: [word for word in words if _find(word, text, allow_plural=True) is not None]
        for area, words in AREA_KEYWORDS.items()
    }

    # Most hits wins, and each flag key mentioned is one more hit for feature_flags.
    # max() keeps the first of any tie, so going in priority order settles ties.
    hits = {name: len(matches[name]) for name in TIE_PRIORITY}
    hits["feature_flags"] += len(flag_keys)
    area = max(TIE_PRIORITY, key=lambda name: hits[name])
    if not hits[area]:
        area = "general"

    severity = 0
    if str(ticket.get("plan") or "").lower() == "enterprise":
        severity += 1
    if any(_find(word, text) is not None for word in URGENT_WORDS):
        severity += 1

    return {
        "area": area,
        "team": AREA_TEAMS[area],
        "severity": SEVERITY_LABELS[severity],
        "flag_key": flag_keys[0] if flag_keys else None,
        "keywords": _search_keywords(matches.get(area, []), known_keys),
        # Without the flag keys, a ticket about a flag can land in the wrong area.
        "flag_lookup_failed": known_keys is None,
    }
