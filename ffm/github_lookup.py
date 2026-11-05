"""Extra pointers for a support ticket: related GitHub issues (C9) and docs to read (C10).

These checks only offer pointers, so they always return status info, even when a lookup fails.
"""
from urllib.parse import urlencode

import pandas as pd
import requests

from ffm.checks import CheckResult
from ffm.classify import AREA_KEYWORDS
from ffm.config import GITHUB_TOKEN

GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
GITHUB_REPO = "PostHog/posthog"

# Stop waiting for GitHub after this many seconds.
GITHUB_TIMEOUT_SECONDS = 10

# How many keywords to search with, and how many issues to list.
MAX_KEYWORDS = 4
MAX_ISSUES = 5

# Fixed search words for checks about one event. A customer's own event names must never be searched for.
FIXED_SEARCH_WORDS = {"C3": "$pageview", "C6": "$exception", "C7": "duplicate events"}

# PostHog's own SDKs. This is a list because a customer can put any text in $lib, even text starting with "posthog-".
POSTHOG_SDK_NAMES = {
    "posthog-android", "posthog-dotnet", "posthog-elixir", "posthog-flutter", "posthog-go", "posthog-ios",
    "posthog-java", "posthog-js", "posthog-js-lite", "posthog-node", "posthog-php", "posthog-python",
    "posthog-react-native", "posthog-ruby", "posthog-rs",
}

# The GitHub search is public, so these are the only terms that may ever be sent to it.
SAFE_SEARCH_TERMS = (
    {word for words in AREA_KEYWORDS.values() for word in words}
    | POSTHOG_SDK_NAMES
    | set(FIXED_SEARCH_WORDS.values())
)

# The docs page to read for each area from ffm/classify.py. Edit these freely.
DOCS_MAP = {
    "feature_flags": "https://posthog.com/docs/feature-flags",
    "error_tracking": "https://posthog.com/docs/error-tracking",
    "identification": "https://posthog.com/docs/product-analytics/identify",
    "data_quality": "https://posthog.com/docs/libraries/js",
    # This page has a section on why events might not be appearing in a project.
    "ingestion": "https://posthog.com/docs/product-analytics/troubleshooting",
    "general": "https://posthog.com/docs",
}


def _search_text(keywords):
    """Build the GitHub search. OR finds issues with any keyword, because requiring them all often finds nothing."""
    # Quotes keep a phrase like "unique users" together.
    terms = [f'"{word}"' for word in keywords]
    return f"{' OR '.join(terms)} repo:{GITHUB_REPO} is:issue"


def check_c9_github(keywords, area=None):
    """C9: find PostHog/posthog issues that mention these keywords. Tickets in the general area skip the search.

    This search is public, so anything not in SAFE_SEARCH_TERMS is dropped here, whoever called this.
    Callers must still leave out flag keys, in case a flag is named like one of the safe terms.
    """
    id, name = "C9", "Related GitHub issues"
    next_step = "Read any that match the ticket, to see if this is a known issue or has a workaround."
    columns = ["number", "title", "state", "updated_at", "url"]
    # Filled in below, once we know what's safe. result() reads it whenever it's called.
    sent_terms = []

    def result(headline, evidence=None, query="", next_step=next_step, docs_url=f"https://github.com/{GITHUB_REPO}/issues"):
        """Every outcome of this check has status info, so only these details change."""
        evidence = pd.DataFrame(columns=columns) if evidence is None else evidence
        return CheckResult(id, name, "info", headline, evidence, "", next_step, docs_url, query, search_terms=sent_terms)

    if area == "general":
        return result(
            "No related-issue search: the ticket matched no product area.",
            next_step="Search the PostHog/posthog issues by hand if needed.",
        )

    sent_terms = [word for word in keywords if word in SAFE_SEARCH_TERMS][:MAX_KEYWORDS]
    if not sent_terms:
        return result("No safe search terms", next_step="Search the PostHog/posthog issues by hand if needed.")

    query = _search_text(sent_terms)
    page_url = f"https://github.com/{GITHUB_REPO}/issues?{urlencode({'q': query})}"

    # Without a token GitHub still answers, but allows fewer searches per minute.
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(
            GITHUB_SEARCH_URL,
            params={"q": query, "per_page": MAX_ISSUES},
            headers=headers,
            timeout=GITHUB_TIMEOUT_SECONDS,
        )
        # GitHub uses 403, and sometimes 429, to say we've searched too often.
        if response.status_code in (403, 429):
            return result(
                "GitHub search limit reached, try again in a minute",
                query=query, next_step="Run this check again in a minute.", docs_url=page_url,
            )
        if response.status_code == 401:
            return result(
                "GitHub search could not run: GitHub rejected the token. Check GITHUB_TOKEN in .env",
                query=query, next_step="Fix GITHUB_TOKEN in .env, or remove it.", docs_url=page_url,
            )
        response.raise_for_status()
        data = response.json()
        total = data["total_count"]
        rows = [
            {
                "number": issue["number"],
                "title": issue["title"],
                "state": issue["state"],
                "updated_at": issue["updated_at"],
                "url": issue["html_url"],
            }
            for issue in data["items"]
        ]
    # A failed search shouldn't stop the other checks. Odd replies raise ValueError or KeyError.
    except (requests.RequestException, ValueError, KeyError) as error:
        return result(
            f"GitHub search could not run: {type(error).__name__}",
            query=query, next_step="Run this check again, or use the search link.", docs_url=page_url,
        )

    keyword_list = ", ".join(sent_terms)
    if total == 0:
        return result(f"No issues on {GITHUB_REPO} mention {keyword_list}", query=query, docs_url=page_url)
    return result(
        f"{total:,} issues on {GITHUB_REPO} mention {keyword_list}, showing the {len(rows)} closest",
        pd.DataFrame(rows, columns=columns), query, docs_url=page_url,
    )


def check_c10_docs(area):
    """C10: point to the docs page for a product area."""
    # An area we don't know about gets the main docs page.
    url = DOCS_MAP.get(area, DOCS_MAP["general"])
    return CheckResult(
        "C10", "Docs to read", "info", f"Docs to read for {area}: {url}",
        pd.DataFrame({"area": [area], "url": [url]}), "",
        "Read this page and link the relevant section in your reply.", url, "",
    )
