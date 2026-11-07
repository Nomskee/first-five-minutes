// First Five Minutes Slack bot.
// When someone adds a :ticket: reaction to a message, the bot sends that message to the
// diagnostics API (api.py) and posts what the checks found in the triage channel.
// Start it with: npm start

import path from "node:path";
import { fileURLToPath } from "node:url";

import { App, type Logger, type types } from "@slack/bolt";
import { config } from "dotenv";

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

// The .env file sits in the project folder, one level above slack-bot.
// import.meta.dirname is the folder this file is in (slack-bot/src), so this works whatever folder you start from.
config({ path: path.join(import.meta.dirname, "..", "..", ".env"), quiet: true });

// Only this reaction starts a diagnosis. Slack names the 🎫 emoji "ticket".
const TICKET_REACTION = "ticket";

// Give up on the API after 60 seconds. A slow PostHog can make a run take a while.
const API_TIMEOUT_MS = 60_000;

// Slack refuses a text section longer than this.
const MAX_SECTION_LENGTH = 3000;

const FOOTER = "Internal note. Write the customer reply yourself.";
const API_DOWN_MESSAGE = "Diagnostics API isn't running. Start api.py";

// The same wording as in ffm/diagnose.py, so the bot and the note agree.
const FLAG_LOOKUP_WARNING = "Couldn't load feature flags, so the area and team may be wrong.";

// The icon shown in front of each check, by status.
const STATUS_ICONS: Record<string, string> = { fail: "🔴", warn: "🟠", error: "⚪" };

/** Read a setting from .env, and stop with a clear message if it's missing. */
function requireSetting(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is missing. Add it to the .env file in the project folder.`);
  }
  return value;
}

// ---------------------------------------------------------------------------
// The API's reply. These match ffm/diagnose.py's to_json(), so TypeScript can
// warn us if we use a field that isn't there.
// ---------------------------------------------------------------------------

/** One check's outcome, such as C1 "SDK went quiet". */
interface CheckResult {
  id: string; // "C1" to "C10"
  name: string; // a short name, such as "SDK went quiet"
  status: "pass" | "warn" | "fail" | "info" | "error";
  headline: string; // one sentence with the actual numbers
  evidence: Record<string, unknown>[]; // the table behind the headline, one object per row
  likely_cause: string;
  next_step: string;
  docs_url: string;
  query: string; // the HogQL that was run, or the GitHub search for C9
  relevant: boolean; // does this check matter for this ticket's area?
  seconds: number; // how long the check took
  retried: boolean; // did PostHog need a second try?
  search_terms: string[]; // the exact words sent to GitHub. Only C9 fills this in
}

/** What the ticket is about, worked out from its words. */
interface Classification {
  area: string; // such as "feature_flags", or "general" if nothing matched
  team: string; // the team that handles that area
  severity: "low" | "medium" | "high";
  flag_key: string | null; // a feature flag the ticket mentions, if any
  keywords: string[];
  flag_lookup_failed: boolean; // true if PostHog's flags couldn't be loaded
}

/** One line of the likely cause. */
interface LikelyCause {
  check: string | null; // the check it comes from, such as "C5 Feature flag can match", or null for a general line
  text: string;
}

/** The whole reply from POST /diagnose. */
interface Diagnosis {
  ticket: { subject: string; body: string; plan: string; channel: string; id: string | null };
  classification: Classification;
  results: CheckResult[]; // already sorted: the most important first
  likely_cause: LikelyCause[]; // the same lines as the note's Likely cause section. Never empty
  note_markdown: string; // the written note. The bot builds its own message instead
  seconds: number; // how long the whole diagnosis took
}

// ---------------------------------------------------------------------------
// Calling the API
// ---------------------------------------------------------------------------

/** Either the diagnosis, or a message saying why there isn't one. */
type ApiOutcome = { ok: true; diagnosis: Diagnosis } | { ok: false; message: string };

/** Send the message to POST /diagnose and return what came back. */
export async function callDiagnoseApi(apiUrl: string, text: string, timeoutMs = API_TIMEOUT_MS): Promise<ApiOutcome> {
  const ticket = {
    // Array.from splits the text into characters, so an emoji is never cut in half.
    subject: Array.from(text).slice(0, 60).join(""),
    body: text,
    plan: "enterprise",
    channel: "slack",
  };

  let response: Response;
  try {
    response = await fetch(`${apiUrl.replace(/\/+$/, "")}/diagnose`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ticket),
      // Cancel the request if it takes longer than the timeout.
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    // A timeout means the API is running but slow. Anything else here means nothing answered at all.
    if (error instanceof Error && error.name === "TimeoutError") {
      return { ok: false, message: `Diagnostics API didn't answer within ${timeoutMs / 1000} seconds. Try again in a minute.` };
    }
    return { ok: false, message: API_DOWN_MESSAGE };
  }

  // The API answered, but with an error, such as 500 if a setting in .env is wrong.
  if (!response.ok) {
    return { ok: false, message: `Diagnostics API returned an error (${response.status}). Check the api.py window.` };
  }
  return { ok: true, diagnosis: (await response.json()) as Diagnosis };
}

// ---------------------------------------------------------------------------
// Building the Slack message
// ---------------------------------------------------------------------------

/** Slack reads <, > and & as formatting, so text from the checks has to escape them. */
function escapeText(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Make a text section, cut short if it's too long for Slack. */
function section(text: string): types.SectionBlock {
  const fits = text.length <= MAX_SECTION_LENGTH ? text : `${text.slice(0, MAX_SECTION_LENGTH - 1)}…`;
  return { type: "section", text: { type: "mrkdwn", text: fits } };
}

/** Small grey text, used for the footer and warnings. */
function context(text: string): types.ContextBlock {
  return { type: "context", elements: [{ type: "mrkdwn", text }] };
}

/** Turn the API's likely-cause lines into Slack text. The API decides what they say. */
function likelyCauseLines(likelyCause: LikelyCause[]): string[] {
  return likelyCause.map((item) =>
    item.check ? `• *${escapeText(item.check)}:* ${escapeText(item.text)}` : `• ${escapeText(item.text)}`,
  );
}

/** Build the reply from the API's JSON. */
export function buildBlocks(diagnosis: Diagnosis, permalink: string | undefined): types.KnownBlock[] {
  const { classification, results } = diagnosis;
  const blocks: types.KnownBlock[] = [];

  // 1. A link back to the message that got the reaction.
  blocks.push(section(permalink ? `*<${permalink}|Original message>*` : "*Original message* (link unavailable)"));

  // 2. Who should handle it, and how urgent it is.
  blocks.push({
    type: "section",
    fields: [
      { type: "mrkdwn", text: `*Suggested group*\n${escapeText(classification.team)}` },
      { type: "mrkdwn", text: `*Severity*\n${escapeText(classification.severity)}` },
    ],
  });
  if (classification.flag_lookup_failed) {
    blocks.push(context(`:warning: ${FLAG_LOOKUP_WARNING}`));
  }

  // 3. The likely cause.
  blocks.push(section(`*Likely cause*\n${likelyCauseLines(diagnosis.likely_cause).join("\n")}`));

  // 4. Every check that failed, warned or couldn't run, one line each, most important first.
  // Errors are listed too, because a check that couldn't run might have found something.
  const flagged = results.filter((r) => r.status === "fail" || r.status === "warn" || r.status === "error");
  const line = (r: CheckResult) => `${STATUS_ICONS[r.status]} *${r.id}* ${escapeText(r.headline)}`;

  // "Findings" is about this ticket's area. It's left out when nothing there was flagged,
  // because the likely cause above already says so.
  const findings = flagged.filter((r) => r.relevant).map(line);
  if (findings.length > 0) {
    blocks.push(section(`*Findings*\n${findings.join("\n")}`));
  }

  // "Also found" is everything else, like the note's section of the same name.
  const alsoFound = flagged.filter((r) => !r.relevant).map(line);
  blocks.push(section(`*Also found*\n${alsoFound.length > 0 ? alsoFound.join("\n") : "Nothing else failed."}`));

  // 5. The reminder that a person writes the reply.
  blocks.push(context(FOOTER));
  return blocks;
}

// ---------------------------------------------------------------------------
// Handling a reaction
// ---------------------------------------------------------------------------

/** The Slack Web API client that Bolt gives each handler. */
type SlackClient = App["client"];

/** What the reaction handler needs besides the event itself. */
export interface HandlerSettings {
  triageChannel: string; // where the bot posts. Never the channel where the reaction happened
  apiUrl: string;
  timeoutMs?: number;
}

/** Run the checks on a message that got a :ticket: reaction, and post the results in the triage channel. */
export async function handleReaction(
  event: types.ReactionAddedEvent,
  client: SlackClient,
  logger: Logger,
  settings: HandlerSettings,
): Promise<void> {
  // Ignore every other reaction, and reactions on things that aren't messages, such as files.
  if (event.reaction !== TICKET_REACTION || event.item.type !== "message") {
    return;
  }
  const { channel, ts } = event.item;

  // The bot only ever posts in the triage channel. A reaction there would mean posting in
  // the channel where the reaction happened, so it's ignored.
  if (channel === settings.triageChannel) {
    logger.info("Ignored a :ticket: reaction in the triage channel itself.");
    return;
  }

  /** Tell the triage channel that something went wrong before the checks could start. */
  const reportProblem = async (reason: string) => {
    logger.warn(reason);
    await client.chat.postMessage({ channel: settings.triageChannel, text: `Couldn't run first-five-minutes checks: ${reason}` });
  };

  try {
    // Fetch the message that got the reaction. "latest" plus "inclusive" asks for the message
    // at exactly that time stamp, and limit 1 asks for only that one.
    const history = await client.conversations.history({ channel, latest: ts, inclusive: true, limit: 1 });
    const message = history.messages?.[0];
    // conversations.history doesn't include thread replies. For a reply, Slack returns an
    // earlier message instead, so check that it's really the one that got the reaction.
    if (!message || message.ts !== ts) {
      await reportProblem("the :ticket: was on a thread reply or a message the bot can't see.");
      return;
    }
    const text = message.text ?? "";

    // A link to the original message, for the reply.
    const { permalink } = await client.chat.getPermalink({ channel, message_ts: ts });

    // Say the checks have started, and keep this post's time stamp so the results go in its thread.
    const started = await client.chat.postMessage({ channel: settings.triageChannel, text: "Running first-five-minutes checks…" });
    const threadTs = started.ts;
    // Slack always sends this back for a successful post, but its type allows it to be missing.
    if (!threadTs) {
      throw new Error("Slack didn't say where the post went, so the results can't go in its thread.");
    }

    const outcome = await callDiagnoseApi(settings.apiUrl, text, settings.timeoutMs);
    if (!outcome.ok) {
      await client.chat.postMessage({ channel: settings.triageChannel, thread_ts: threadTs, text: outcome.message });
      return;
    }

    const { classification } = outcome.diagnosis;
    await client.chat.postMessage({
      channel: settings.triageChannel,
      thread_ts: threadTs,
      // Shown in notifications, and by Slack apps that can't show blocks.
      text: `First five minutes: ${classification.team}, ${classification.severity} severity`,
      blocks: buildBlocks(outcome.diagnosis, permalink),
    });
  } catch (error) {
    // For example a missing permission, or the bot not being in the channel ("not_in_channel").
    const reason = error instanceof Error ? error.message : String(error);
    logger.error(`Couldn't handle a :ticket: reaction: ${reason}`);
    try {
      await reportProblem(reason);
    } catch {
      // Posting failed too, so the log above is all we can do.
    }
  }
}

// ---------------------------------------------------------------------------
// Starting the bot
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const settings: HandlerSettings = {
    triageChannel: requireSetting("SLACK_TRIAGE_CHANNEL_ID"),
    apiUrl: requireSetting("FFM_API_URL"),
  };

  // Socket Mode connects out to Slack, so the bot needs no public web address.
  const app = new App({
    token: requireSetting("SLACK_BOT_TOKEN"),
    appToken: requireSetting("SLACK_APP_TOKEN"),
    socketMode: true,
  });

  // Bolt calls this for every reaction the bot can see.
  app.event("reaction_added", async ({ event, client, logger }) => {
    await handleReaction(event, client, logger, settings);
  });

  await app.start();
  app.logger.info("First Five Minutes bot is running. Add a :ticket: reaction to a message to diagnose it.");
}

// Only start the bot when this file is run directly, not when a test imports it.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
