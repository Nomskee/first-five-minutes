"""First Five Minutes as a web API. Start it with: uvicorn api:app"""
from fastapi import FastAPI
from pydantic import BaseModel

from ffm.diagnose import diagnose, to_json

app = FastAPI(title="First Five Minutes")


class Ticket(BaseModel):
    """A support ticket to diagnose."""

    subject: str
    body: str
    plan: str = "paid"
    channel: str = "slack"
    id: str | None = None


@app.get("/health")
def health():
    """Check that the API is running. This doesn't contact PostHog or GitHub."""
    return {"status": "ok"}


# This is "def", not "async def", because diagnose() waits on the network. FastAPI runs a
# plain "def" on a separate thread, so the API can still answer other requests meanwhile.
@app.post("/diagnose")
def diagnose_ticket(ticket: Ticket):
    """Classify a support ticket and run all the checks on it.

    Returns the ticket, its classification, each check's result and an internal note for the person handling it.
    The checks query PostHog and GitHub, so this usually takes a few seconds, and sometimes much longer.
    """
    return to_json(diagnose(ticket.model_dump()))
