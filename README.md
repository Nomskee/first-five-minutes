# First Five Minutes

A diagnostics tool for PostHog support tickets. It runs the checks a support engineer would run in the first five minutes of a ticket, before writing a reply.

## The problem

PostHog's support is done by full-time engineers, not a separate tier-one team. Per the [support handbook](https://posthog.com/handbook/support/customer-support), the team is small and handles a large volume of tickets. Tickets are prioritized by plan, with first-reply targets ranging from 72 hours on pay-as-you-go down to 8 hours on Enterprise.

The [Technical Support Engineer posting](https://posthog.com/careers/technical-support-engineer) asks for someone who:
- resolves most tickets directly instead of passing them on,
- switches context across many products in a day (analytics, feature flags, error tracking, SDKs),
- escalates the right issues to the right product team,
- improves the team's own tooling, automation and reporting.

Many tickets start the same way: before replying, the engineer has to open the customer's project, run the same handful of queries, and find out what is actually wrong. Common findings are an SDK that stopped sending, an outdated library, users who are never identified, or a flag that can't match anyone. The customer's description is often a symptom, not the cause. "Unique users way too high" turns out to be missing `identify()` calls, and the same missing calls are why a feature flag shows for nobody.

First Five Minutes runs those first queries automatically, so the engineer can start from evidence.

## What it does

It reads a support ticket, works out which product area it's about, and runs ten checks against the customer's PostHog project using HogQL. It ranks the results so that the checks for the ticket's own area come first. It also links findings that share a cause and adds related GitHub issues and docs. It produces an internal note with the likely cause and the evidence behind it, in a Streamlit app, through an API, or as a Slack reply in an internal triage channel.

## Screenshot

<!-- TODO: add a screenshot of the app diagnosing T2, e.g. docs/screenshot.png -->
![First Five Minutes diagnosing a ticket](docs/screenshot.png)

## Architecture

- **`ffm/config.py`** reads settings from `.env`. A required setting that's missing stops it with a clear error.
- **`ffm/posthog_api.py`** runs HogQL queries through PostHog's query API and returns pandas DataFrames.
- **`ffm/checks.py`** holds checks C1 to C8. Each returns a `CheckResult` with a status (fail, warn, pass, info or error), a headline, the evidence table, the query it ran, a likely cause and a docs link. Time windows are measured from the anchor (the customer project's newest event), not `now()`, so seeded data stays testable.
- **`ffm/classify.py`** maps a ticket to a product area, team and severity using fixed keyword lists. It also finds feature-flag keys from the customer's project.
- **`ffm/github_lookup.py`** runs C9, the related-issue search on `PostHog/posthog`, and C10, the docs map. The search only ever sends allowlisted words: fixed keywords, PostHog SDK names, `$` events and "duplicate events". Never the customer's own words or flag keys.
- **`ffm/diagnose.py`** runs the pipeline:
  1. classify the ticket;
  2. run C1 to C8 in parallel;
  3. search GitHub;
  4. sort the results, link related findings and mark which are relevant;
  5. write the internal note.

  A check that crashes shows as an error, and the other checks still run.
- **`ffm/telemetry.py`** records each run as `ffm_diagnostic_run` and `ffm_check_result` events in a separate internal PostHog project. Telemetry errors never stop a diagnosis.
- **`app.py`** is the Streamlit app. Choose a sample ticket or paste one, and it shows the likely cause, a card for each check and the internal note.
- **`pages/2_Patterns.py`** lists checks that failed on two or more tickets this week, from telemetry. It drafts a GitHub issue for one of them.
- **`api.py`** is a FastAPI service with `GET /health` and `POST /diagnose`, which returns the diagnosis as JSON.
- **`slack-bot/`** is a TypeScript Slack bot (Bolt, Socket Mode). React to a message with :ticket: and it calls the API and posts the results in the triage channel.
- **`notebooks/`** holds the seeding notebook and the notebook used to build and try the checks.
- **`tickets.json`** has five sample tickets, T1 to T5.

## The ten checks

| ID | Check | Built from | Fail when | Warn when | Planted problem |
| --- | --- | --- | --- | --- | --- |
| C1 | SDK went quiet | Q1 | One SDK silent 24 h or more longer than the most recently active SDK | Silent 6 to 24 h longer | P1 |
| C2 | SDK version freshness | Q2 + latest version from npm or PyPI | 50 or more minor releases behind latest | 10 to 49 behind | P5 |
| C3 | Missing core properties | Q3 | Over 10% of $pageview have no $current_url | 1% to 10% | P5 |
| C4 | Identification health | Q4 | An SDK has over 500 distinct IDs and no $identify calls | Identify calls under 1% of distinct IDs | P3 |
| C5 | Feature flag can match | Q5 + Feature flags API | Flag inactive, or no person matches its person-property condition | Every $feature_flag_called in 7 days returned false | P2 |
| C6 | Error spike | Q6 + 13-day daily average | Last 24 h over 5x the daily average and over 50 errors | 2x to 5x the average | P4 |
| C7 | Duplicate events | Q7 | Over 10% of an event's rows are exact duplicates | 1% to 10% | P6 |
| C8 | Overall volume drop | Last 24 h vs 7-day daily average | Under 25% of the average | 25% to 50% | None, should pass |
| C9 | Related GitHub issues | GitHub search on PostHog/posthog | Never fails | Always info | All |
| C10 | Docs to read | Fixed map of product area to docs page | Never fails | Always info | All |

The queries Q1 to Q7 and the expected result for each ticket are in [queries.md](queries.md).

## How to run it

You need Python 3, Node.js, and a PostHog Cloud account with two projects: a **customer** project, which holds the seeded data, and an **internal** project, which holds the tool's own telemetry.

**Stage 0: Setup.**
1. Create a `.env` file in the project root with these settings:
   - `POSTHOG_HOST`, `POSTHOG_INGEST_HOST`
   - `CUSTOMER_PROJECT_ID`, `CUSTOMER_PROJECT_TOKEN`
   - `INTERNAL_PROJECT_ID`, `INTERNAL_PROJECT_TOKEN`
   - `POSTHOG_PERSONAL_API_KEY`
   - optional: `GITHUB_TOKEN`, `GITHUB_USERNAME`, `ANCHOR_MODE`
2. Install the Python packages:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -r requirements.txt
```

(On macOS or Linux, use `.venv/bin/` instead of `.venv/Scripts/`.)

**Stage 1: Seed the customer.** Run `notebooks/01_seed_customer.ipynb`. It sends 14 days of events for Tidewater, a fictional ticketing company, with six planted problems (P1 to P6). Then, in the customer project, create an active `new-checkout` feature flag that targets persons whose `email` contains `@tidewater.io`. C5 reads it through the API.

**Stage 2: Find the problems by hand.** The HogQL queries in `queries.md` find each planted problem. Run them in PostHog's SQL editor to see the raw evidence.

**Stage 3: Turn the queries into checks.** `ffm/checks.py` wraps them as C1 to C8 with fail and warn thresholds. `notebooks/02_try_checks.ipynb` runs each check.

**Stage 4: Read the ticket.** `ffm/classify.py` picks the area, team and severity. `ffm/github_lookup.py` adds C9 and C10.

**Stage 5: Diagnose.** `ffm/diagnose.py` puts it together. For a quick check from Python:

```bash
.venv/Scripts/python -c "import json; from ffm.diagnose import diagnose; print(diagnose(json.load(open('tickets.json'))[0])['note_markdown'])"
```

Every diagnosis, including this one, is recorded in the internal project's telemetry.

**Stage 6: The app and the API.**

```bash
.venv/Scripts/streamlit run app.py
```

```bash
.venv/Scripts/uvicorn api:app --port 8000
```

The API docs are at http://localhost:8000/docs.

**Stage 7: The Slack bot.**
1. Create a Slack app with Socket Mode on.
2. Add `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_TRIAGE_CHANNEL_ID` and `FFM_API_URL` to `.env`.
3. Start the API, then:

```bash
cd slack-bot && npm install && npm start
```

React to a ticket message with :ticket:. The results appear in the triage channel.

In VS Code, the **Run everything** launch configuration starts the app, the API and the bot together.

## Honest limits

- **The data is synthetic.** Tidewater and its six problems were planted by the seeding notebook, and the five tickets were written to match them. Real projects are messier, with more SDKs, more events, and problems that overlap.
- **The thresholds are my assumptions.** "24 hours quiet", "50 minor versions behind" and "5x the daily average" are reasonable starting points. They haven't been tuned against real tickets, so expect false alarms and misses.
- **The tool never writes customer replies.** It writes an internal note for the support engineer, marked "Internal note. Write the customer reply yourself." The engineer decides what to tell the customer.
- **Slack output goes to an internal channel only.** The bot posts only in the triage channel set in `.env`, never in the channel where the reaction happened. So nothing reaches a customer-facing channel.

## What I'd do next

- **Tune thresholds on real tickets.** Run the checks on a sample of past tickets with known causes, then set each threshold to balance false alarms against misses.
- **Add session replay and data warehouse checks.** For example: recordings not being captured, replay sampling or minimum-duration settings that drop most sessions, and warehouse sources whose syncs are failing or stale.
- **Run it inside PostHog's own support tooling.** Trigger it from the ticketing system when a ticket arrives, so the note is waiting before the engineer opens the ticket. Feed the Patterns page's repeated causes back to the product teams.
