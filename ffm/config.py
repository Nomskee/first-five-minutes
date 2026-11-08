"""Settings for the project, read from the .env file in the project root."""
import os
from pathlib import Path

from dotenv import load_dotenv

# This file is ffm/config.py, so two .parent steps lead to the project root.
# Building the path this way finds .env no matter where you run Python from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Copy the values from .env into the environment so os.getenv can read them.
# A variable already set in your shell wins over the one in .env.
load_dotenv(PROJECT_ROOT / ".env")


def _required(name):
    """Return the value of a setting, or stop with a clear error if it's empty."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {name}. Add it to your .env file.")
    return value


# PostHog addresses: the app and API, and where events are sent.
POSTHOG_HOST = _required("POSTHOG_HOST")
POSTHOG_INGEST_HOST = _required("POSTHOG_INGEST_HOST")

# The customer project holds the seeded events.
CUSTOMER_PROJECT_ID = _required("CUSTOMER_PROJECT_ID")
CUSTOMER_PROJECT_TOKEN = _required("CUSTOMER_PROJECT_TOKEN")

# The internal project is for our own tracking.
INTERNAL_PROJECT_ID = _required("INTERNAL_PROJECT_ID")
INTERNAL_PROJECT_TOKEN = _required("INTERNAL_PROJECT_TOKEN")

# The personal API key lets us read data through the PostHog API. Keep it secret.
POSTHOG_PERSONAL_API_KEY = _required("POSTHOG_PERSONAL_API_KEY")

# Optional: this is an empty string if it isn't set, and nothing raises an error.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# Optional: the GitHub account that owns the first-five-minutes repo. The Patterns page links to its issues.
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "").strip()

# What check time windows are measured from: "latest_event" (the newest event in the project) or "now".
# Optional: if it isn't set, we use latest_event.
ANCHOR_MODE = os.getenv("ANCHOR_MODE", "").strip() or "latest_event"
if ANCHOR_MODE not in ("latest_event", "now"):
    raise RuntimeError(f"ANCHOR_MODE must be latest_event or now, not {ANCHOR_MODE}. Fix it in your .env file.")
