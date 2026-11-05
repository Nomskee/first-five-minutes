"""Small helpers for reading data from the PostHog API."""
import threading
import time

import pandas as pd
import requests

from ffm.config import CUSTOMER_PROJECT_ID, POSTHOG_HOST, POSTHOG_PERSONAL_API_KEY

# Stop waiting if PostHog hasn't answered after this many seconds.
TIMEOUT_SECONDS = 30

# How long to wait before trying a request a second time.
RETRY_WAIT_SECONDS = 2

# Remembers whether a retry happened. Each thread gets its own copy, so checks running together don't mix them up.
_thread_state = threading.local()


def clear_retried():
    """Forget any earlier retry on this thread. Call this before starting something new."""
    _thread_state.retried = False


def was_retried():
    """Return True if a request on this thread was retried since clear_retried()."""
    return getattr(_thread_state, "retried", False)


def _send_with_retry(send):
    """Call send() to make a request, and try once more if PostHog is busy or having a problem."""
    response = send()
    # 429 means too many requests, and 5xx means a problem on PostHog's side. Both can pass in a moment.
    if response.status_code == 429 or response.status_code >= 500:
        _thread_state.retried = True
        time.sleep(RETRY_WAIT_SECONDS)
        response = send()
    return response


def _check_response(response, scope):
    """Stop with a clear error if PostHog refused the request. `scope` is the permission it needs."""
    # 401 means PostHog doesn't accept the key at all. The query endpoint answers a wrong key
    # with 403 instead, so we also look for "authentication_error" in the reply.
    if response.status_code == 401 or (response.status_code == 403 and "authentication_error" in response.text):
        raise RuntimeError(
            f"PostHog returned {response.status_code}: the personal API key is wrong. "
            "Check POSTHOG_PERSONAL_API_KEY in .env."
        )
    # Any other 403 means the key is valid but isn't allowed to do this.
    if response.status_code == 403:
        raise RuntimeError(f"PostHog returned 403: the personal API key is missing a scope, probably {scope}.")
    # Any other 4xx or 5xx code is also a failure. PostHog's message says why, for example a typo in the query.
    if not response.ok:
        raise RuntimeError(f"PostHog returned {response.status_code}: {response.text}")


def _headers():
    """Build the header that proves who is calling."""
    return {"Authorization": f"Bearer {POSTHOG_PERSONAL_API_KEY}"}


def run_hogql(query, project_id=CUSTOMER_PROJECT_ID):
    """Run a HogQL (PostHog's SQL) query and return the answer as a table."""
    # PostHog expects the SQL text wrapped in an object that says what kind of query it is.
    response = _send_with_retry(lambda: requests.post(
        f"{POSTHOG_HOST}/api/projects/{project_id}/query/",
        headers=_headers(),
        json={"query": {"kind": "HogQLQuery", "query": query}},
        timeout=TIMEOUT_SECONDS,
    ))
    _check_response(response, "query:read")

    # The reply has the column names in "columns" and the rows in "results".
    data = response.json()
    return pd.DataFrame(data["results"], columns=data["columns"])


def get_feature_flags(project_id=CUSTOMER_PROJECT_ID):
    """Return the project's feature flags as a list of dictionaries."""
    response = _send_with_retry(lambda: requests.get(
        f"{POSTHOG_HOST}/api/projects/{project_id}/feature_flags/",
        headers=_headers(),
        timeout=TIMEOUT_SECONDS,
    ))
    _check_response(response, "feature_flag:read")

    # The flags themselves are in the "results" part of the reply.
    return response.json()["results"]
