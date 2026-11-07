"""Record each diagnosis in our own internal PostHog project, so we can see how the tool is used.

This goes to the internal project, never the customer's. The checks also skip every event named ffm_...
"""
import hashlib
from functools import lru_cache

from posthog import Posthog

from ffm.config import INTERNAL_PROJECT_TOKEN, POSTHOG_INGEST_HOST

# Every event is recorded as coming from this one person: the support engineer running the tool.
DISTINCT_ID = "support-engineer-onome"

# Stop waiting for PostHog to accept the events after this many seconds.
FLUSH_TIMEOUT_SECONDS = 10


def _warn(message):
    """Telemetry problems are only worth a warning. They must never stop a diagnosis."""
    print(f"Warning: telemetry failed: {message}")


@lru_cache(maxsize=None)
def _client():
    """Create the PostHog client once, and reuse it for every diagnosis."""
    return Posthog(
        INTERNAL_PROJECT_TOKEN,
        host=POSTHOG_INGEST_HOST,
        # The client sends events from a background thread, so a failed send can't raise an
        # error here. This makes it print a warning instead of failing silently.
        on_error=lambda error, _batch: _warn(error),
    )


def ticket_id(ticket):
    """The ticket's own id, or for a pasted ticket with none, adhoc- plus the start of an md5 of its body.

    The same pasted text always gets the same id, so repeat runs of one ticket can be grouped.
    """
    if ticket.get("id"):
        return ticket["id"]
    body = ticket.get("body") or ""
    return "adhoc-" + hashlib.md5(body.encode("utf-8")).hexdigest()[:8]


def _top_check(results):
    """The id of the first failed or warned check, or None. diagnose() has already put the most important first."""
    for result in results:
        if result.status in ("fail", "warn"):
            return result.id
    return None


def log_diagnosis(diagnosis, source):
    """Send one ffm_diagnostic_run event, and one ffm_check_result event per check.

    `source` says where the diagnosis was run from: "app" or "slack".
    """
    try:
        ticket, results = diagnosis["ticket"], diagnosis["results"]
        classification = diagnosis["classification"]
        id = ticket_id(ticket)
        client = _client()

        client.capture(
            "ffm_diagnostic_run",
            distinct_id=DISTINCT_ID,
            properties={
                "ticket_id": id,
                "ticket_subject": ticket.get("subject") or "",
                "area": classification["area"],
                "severity": classification["severity"],
                "source": source,
                "seconds": diagnosis["seconds"],
                "fails": sum(result.status == "fail" for result in results),
                "top_check": _top_check(results),
            },
        )
        for result in results:
            client.capture(
                "ffm_check_result",
                distinct_id=DISTINCT_ID,
                properties={"ticket_id": id, "check_id": result.id, "status": result.status, "relevant": result.relevant},
            )

        # Send now, instead of waiting for the background thread's next batch.
        client.flush(timeout_seconds=FLUSH_TIMEOUT_SECONDS)
    except Exception as error:
        _warn(error)
