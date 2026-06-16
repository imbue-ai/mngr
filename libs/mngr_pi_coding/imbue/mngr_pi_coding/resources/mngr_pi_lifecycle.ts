// mngr lifecycle extension for the pi coding agent.
//
// pi exposes no shell-hook mechanism (the surface claude/agy use): its only
// lifecycle-event surface is the TypeScript extension API. mngr provisions this
// single extension and loads it with `pi -e <path>` (see plugin.py's
// assemble_command). It is the pi analogue of mngr_antigravity's hook scripts,
// collapsed into one in-process module, and it does three jobs:
//
//   1. Readiness sentinel. On `session_start` (which fires at TUI startup,
//      before any prompt and even with no model configured) it writes
//      `$MNGR_AGENT_STATE_DIR/pi_session_started`. The plugin waits on that file
//      to know the agent can accept its first message -- more robust than
//      scraping a banner string out of the pane.
//
//   2. The RUNNING/WAITING marker. mngr's BaseAgent reports RUNNING iff
//      `$MNGR_AGENT_STATE_DIR/active` exists while the pi process is alive (see
//      determine_lifecycle_state). pi maintains no such file, so this extension
//      touches it on `agent_start` and removes it on `agent_end`. No child/root
//      gating is needed: pi has no in-process subagent/Task tool, so only one
//      agent loop ever runs per process, and only the mngr-launched pi runs this
//      extension (loaded via the explicit `-e` flag, not auto-discovery) -- a
//      nested pi the agent spawns with the bash tool (bare `pi`, no `-e`) never
//      executes these handlers and never touches the marker.
//
//   3. Transcript emission. On `message_end` it appends the raw pi message to
//      `$MNGR_AGENT_STATE_DIR/logs/<type>_transcript/events.jsonl` and, when
//      `MNGR_PI_EMIT_COMMON_TRANSCRIPT=1`, a record in mngr's agent-agnostic
//      common envelope to
//      `$MNGR_AGENT_STATE_DIR/events/<type>/common_transcript/events.jsonl`,
//      which `mngr transcript` reads. Emitting straight from the structured
//      events avoids re-parsing pi's tree-structured session JSONL.
//
// Design rules:
//   * Every handler body is wrapped so a bug here can never disrupt pi's loop.
//   * All filesystem work is synchronous (node's *Sync calls), so ordering is
//     deterministic within pi's single-threaded event loop -- no interleaved
//     appends, no async races on the marker.
//   * No imports from the pi package or any other dependency: the file is
//     provisioned standalone and must load under jiti regardless of where pi
//     itself is installed (npm, brew, bundled binary). Event/message shapes are
//     declared locally as the minimal structural types we read.

import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

// --- Minimal structural types for the bits of pi we read. -------------------
// These mirror pi's public AgentMessage / event shapes (see pi docs/session.md)
// but are declared locally to avoid a build-time dependency on the pi package.

interface TextBlock {
  type: "text";
  text: string;
}
interface ToolCallBlock {
  type: "toolCall";
  id: string;
  name: string;
  arguments: unknown;
}
type ContentBlock = TextBlock | ToolCallBlock | { type: string; [key: string]: unknown };

interface PiUsage {
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  // pi computes per-message cost client-side; `total` is the message's USD cost
  // (verified live -- input/output/cacheRead/cacheWrite + total). Used by the
  // usage writer below; authoritative over any token-derived estimate.
  cost?: { total?: number };
}
interface UserMessage {
  role: "user";
  content: string | ContentBlock[];
  timestamp?: number;
}
interface AssistantMessage {
  role: "assistant";
  content: ContentBlock[];
  model?: string;
  provider?: string;
  usage?: PiUsage;
  stopReason?: string;
  timestamp?: number;
}
interface ToolResultMessage {
  role: "toolResult";
  toolCallId: string;
  toolName: string;
  content: string | ContentBlock[];
  isError?: boolean;
  timestamp?: number;
}
type AgentMessage =
  | UserMessage
  | AssistantMessage
  | ToolResultMessage
  | { role: string; timestamp?: number; [key: string]: unknown };

interface MessageEndEvent {
  message: AgentMessage;
}

interface SessionManager {
  getSessionFile?: () => string | undefined;
}
interface ExtensionContext {
  sessionManager?: SessionManager;
}

// pi's ExtensionAPI -- `on` plus `sendUserMessage` (used to inject input without
// tmux keystrokes). `sendUserMessage` is optional so a stub/fake `pi` (the test
// harness) still type-checks; the inbox watcher guards on its presence.
interface PiApi {
  on: (event: string, handler: (event: any, ctx: ExtensionContext) => void | Promise<void>) => void;
  sendUserMessage?: (content: string, options?: { deliverAs?: "steer" | "followUp" }) => void | Promise<void>;
}

// --- Constants kept in sync with plugin.py / base_agent.py. -----------------

const ACTIVE_MARKER_NAME = "active";
const SESSION_STARTED_SENTINEL_NAME = "pi_session_started";
const SESSION_FILE_NAME = "pi_session_file";
// mngr appends one JSON-encoded message string per line here; we inject each new
// line into the live session via pi.sendUserMessage (no tmux keystrokes). Kept
// in sync with _INBOX_FILE_NAME in plugin.py.
const INBOX_NAME = "pi_inbox";
const INBOX_POLL_MS = 200;

const INPUT_PREVIEW_LIMIT = 200;
const TOOL_OUTPUT_LIMIT = 2000;

// --- Helpers. ---------------------------------------------------------------

// Best-effort log to stderr only; pi treats extension stderr as diagnostic, not
// as agent input. Wrapped so logging itself can never throw.
function logDiagnostic(label: string, error: unknown): void {
  try {
    process.stderr.write(`[mngr_pi_lifecycle] ${label} failed: ${String(error)}\n`);
  } catch {
    // Give up silently -- nothing we can safely do here.
  }
}

function safe(label: string, fn: () => void): void {
  try {
    fn();
  } catch (error) {
    // Never let a lifecycle/transcript failure disrupt pi.
    logDiagnostic(label, error);
  }
}

function appendLine(filePath: string, line: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
  appendFileSync(filePath, line + "\n");
}

function countLines(filePath: string): number {
  if (!existsSync(filePath)) {
    return 0;
  }
  const content = readFileSync(filePath, "utf-8");
  if (content.length === 0) {
    return 0;
  }
  // Trailing newline terminates the last record; splitting a non-empty,
  // newline-terminated file on "\n" yields one empty trailing element.
  const parts = content.split("\n");
  if (parts[parts.length - 1] === "") {
    parts.pop();
  }
  return parts.length;
}

function truncate(text: string, limit: number): string {
  return text.length > limit ? text.slice(0, limit) + "..." : text;
}

function isoTimestamp(message: AgentMessage): string {
  const ms = typeof message.timestamp === "number" ? message.timestamp : Date.now();
  return new Date(ms).toISOString();
}

function textFromContent(content: string | ContentBlock[] | undefined): string {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  return content
    .filter((block): block is TextBlock => block != null && (block as ContentBlock).type === "text")
    .map((block) => block.text)
    .join("");
}

function toolCallsFromContent(content: ContentBlock[] | undefined): Array<Record<string, unknown>> {
  if (!Array.isArray(content)) {
    return [];
  }
  const calls: Array<Record<string, unknown>> = [];
  for (const block of content) {
    if (block != null && (block as ContentBlock).type === "toolCall") {
      const call = block as ToolCallBlock;
      calls.push({
        tool_call_id: call.id,
        tool_name: call.name,
        input_preview: truncate(JSON.stringify(call.arguments ?? {}), INPUT_PREVIEW_LIMIT),
      });
    }
  }
  return calls;
}

// Ordered text/tool_call segments of an assistant turn, preserving the source
// interleaving (unlike the flat text + tool_calls split). Unknown block types
// (thinking, image, ...) carry no transcript-visible content and are skipped.
function partsFromContent(content: ContentBlock[] | undefined): Array<Record<string, unknown>> {
  if (!Array.isArray(content)) {
    return [];
  }
  const parts: Array<Record<string, unknown>> = [];
  for (const block of content) {
    if (block == null) {
      continue;
    }
    const blockType = (block as ContentBlock).type;
    if (blockType === "text") {
      const text = (block as TextBlock).text;
      if (text) {
        parts.push({ type: "text", content: text });
      }
    } else if (blockType === "toolCall") {
      const call = block as ToolCallBlock;
      parts.push({
        type: "tool_call",
        tool_call_id: call.id,
        tool_name: call.name,
        input_preview: truncate(JSON.stringify(call.arguments ?? {}), INPUT_PREVIEW_LIMIT),
      });
    }
  }
  return parts;
}

// --- Extension. -------------------------------------------------------------

export default function mngrPiLifecycle(pi: PiApi): void {
  const stateDir = process.env.MNGR_AGENT_STATE_DIR;
  if (!stateDir) {
    // Not running under mngr; do nothing rather than scatter files.
    return;
  }

  const agentType = process.env.MNGR_PI_AGENT_TYPE || "pi-coding";
  const emitCommon = process.env.MNGR_PI_EMIT_COMMON_TRANSCRIPT === "1";
  const emitRaw = process.env.MNGR_PI_EMIT_RAW_TRANSCRIPT !== "0";

  const markerPath = join(stateDir, ACTIVE_MARKER_NAME);
  const sentinelPath = join(stateDir, SESSION_STARTED_SENTINEL_NAME);
  const sessionFilePath = join(stateDir, SESSION_FILE_NAME);
  const rawPath = join(stateDir, "logs", `${agentType}_transcript`, "events.jsonl");
  const commonPath = join(stateDir, "events", agentType, "common_transcript", "events.jsonl");
  const commonSource = `${agentType}/common_transcript`;

  // Usage events (per-message cost/tokens for `mngr usage`). Written only when
  // mngr_pi_coding_usage provisioned its gate marker -- that package ships the
  // reader claiming the "pi-coding" source, so emitting without it would let
  // `mngr usage` mis-aggregate pi's per-message events. The source is the fixed
  // harness id "pi-coding" (not agentType), so usage from any pi subtype lumps
  // together. Kept in sync with mngr_pi_coding_usage's USAGE_GATE_FILENAME /
  // USAGE_SOURCE_NAME.
  const emitUsage = existsSync(join(stateDir, "pi_emit_usage"));
  const usagePath = join(stateDir, "events", "pi-coding", "usage", "events.jsonl");
  let usageSeq = emitUsage ? countLines(usagePath) : 0;

  // event_id must be unique within commonPath so `mngr transcript`'s dedupe set
  // never drops a real record. Seed the counter from the existing line count so
  // ids keep climbing across stop/start (a `--continue` restart reuses the same
  // session id but only fires message_end for *new* messages, so a per-session
  // reset would collide with ids written before the restart).
  let commonSeq = emitCommon ? countLines(commonPath) : 0;

  // Record this (main) agent's session file so the plugin can resume it
  // explicitly with `pi --session <file>` -- more robust than `--continue`,
  // whose "most recent session for this cwd" can be a session a nested pi (run
  // by the bash tool) created in the same per-agent dir. Only the mngr-launched
  // pi loads this extension (via `-e`), so a nested pi never overwrites this.
  // Updated on /new and /resume (session_switch) so it always names the live
  // session. In-memory sessions (`--no-session`) have no file; leave it as is.
  const recordSessionFile = (ctx: ExtensionContext): void => {
    const file = (() => {
      try {
        return ctx.sessionManager?.getSessionFile?.() ?? "";
      } catch {
        return "";
      }
    })();
    if (file) {
      writeFileSync(sessionFilePath, file);
    }
  };

  // Input injection. mngr delivers messages by appending one JSON-encoded string
  // per line to <state>/pi_inbox; we inject each new line via pi.sendUserMessage
  // so the agent receives input without tmux keystroke simulation, while the TUI
  // stays viewable. The offset is seeded from the current line count *now* (at
  // load, before session_start writes the readiness sentinel mngr waits on), so
  // a resumed restart never re-injects the prior session's already-delivered
  // messages, and -- because mngr only writes after seeing the sentinel -- no
  // message sent right after readiness is skipped.
  const inboxPath = join(stateDir, INBOX_NAME);
  let processedInbox = countLines(inboxPath);
  const drainInbox = (): void => {
    safe("inbox", () => {
      if (typeof pi.sendUserMessage !== "function" || !existsSync(inboxPath)) {
        return;
      }
      const lines = readFileSync(inboxPath, "utf-8").split("\n");
      const total = lines[lines.length - 1] === "" ? lines.length - 1 : lines.length;
      while (processedInbox < total) {
        const raw = lines[processedInbox];
        if (raw !== "") {
          let content: unknown;
          try {
            content = JSON.parse(raw);
          } catch {
            // Skip a malformed line rather than inject garbage or stall.
            processedInbox++;
            continue;
          }
          if (typeof content === "string") {
            // Delivery is best-effort. pi.sendUserMessage is async (returns a
            // Promise), so the offset advances right after the call is initiated
            // (line below), not after the message actually lands -- an async
            // rejection is logged and the message is not retried. A *synchronous*
            // throw, by contrast, propagates before the offset advances and so
            // retries on the next tick. We must attach a rejection handler: a
            // bare `void promise` would surface as an unhandled rejection, which
            // on modern Node terminates the process and would take pi down with
            // it (the one thing this extension must never do).
            const sent = pi.sendUserMessage(content, { deliverAs: "followUp" });
            if (sent != null && typeof (sent as Promise<void>).catch === "function") {
              (sent as Promise<void>).catch((error) => logDiagnostic("inbox inject", error));
            }
          }
        }
        processedInbox++;
      }
    });
  };
  const inboxTimer = setInterval(drainInbox, INBOX_POLL_MS);
  if (typeof inboxTimer.unref === "function") {
    inboxTimer.unref();
  }

  pi.on("session_start", (_event, ctx) => {
    safe("session_start", () => {
      mkdirSync(dirname(sentinelPath), { recursive: true });
      writeFileSync(sentinelPath, "1");
      recordSessionFile(ctx);
    });
  });

  pi.on("session_switch", (_event, ctx) => {
    safe("session_switch", () => {
      recordSessionFile(ctx);
    });
  });

  pi.on("agent_start", (_event, _ctx) => {
    safe("agent_start", () => {
      writeFileSync(markerPath, "1");
    });
  });

  pi.on("agent_end", (_event, _ctx) => {
    safe("agent_end", () => {
      rmSync(markerPath, { force: true });
    });
  });

  pi.on("session_shutdown", (_event, _ctx) => {
    safe("session_shutdown", () => {
      clearInterval(inboxTimer);
      // The process is exiting; mngr will report STOPPED regardless, but clear
      // the marker so a quick relaunch never sees a stale RUNNING.
      rmSync(markerPath, { force: true });
    });
  });

  pi.on("message_end", (event: MessageEndEvent, _ctx) => {
    safe("message_end", () => {
      const message = event?.message;
      if (message == null || typeof message.role !== "string") {
        return;
      }
      if (emitRaw) {
        appendLine(rawPath, JSON.stringify({ type: "message", timestamp: isoTimestamp(message), message }));
      }
      if (emitUsage) {
        // Session id comes from the session file recorded on session_start (which
        // always fires before message_end); reading it is robust to whether this
        // handler's ctx exposes the session manager.
        const sessionFile = (() => {
          try {
            return readFileSync(sessionFilePath, "utf8").trim();
          } catch {
            return "";
          }
        })();
        const usageRecord = toUsageRecord(message, sessionFile, () => `evt-pi-usage-${usageSeq++}`);
        if (usageRecord !== null) {
          appendLine(usagePath, JSON.stringify(usageRecord));
        }
      }
      if (!emitCommon) {
        return;
      }
      const record = toCommonRecord(message, commonSource, () => `pi-${commonSeq++}`);
      if (record !== null) {
        appendLine(commonPath, JSON.stringify(record));
      }
    });
  });
}

// Convert a pi AgentMessage into an mngr usage cost_snapshot record, or null when
// there is nothing to report (non-assistant message, no usage, or no session id).
// pi reports per-message cost (`usage.cost.total`), so this is REPORTED cost; the
// reader sums these per session (session-incremental). `sessionFile` is the live
// pi session file path -- its basename (a timestamp + uuid) is the session id.
export function toUsageRecord(
  message: AgentMessage,
  sessionFile: string,
  nextId: () => string,
): Record<string, unknown> | null {
  if (message.role !== "assistant") {
    return null;
  }
  const assistant = message as AssistantMessage;
  const usage = assistant.usage;
  if (!usage) {
    return null;
  }
  const sessionId = sessionFile ? basename(sessionFile, ".jsonl") : "";
  if (!sessionId) {
    return null;
  }
  const cost = usage.cost?.total;
  const hasCost = typeof cost === "number";
  const hasTokens =
    usage.input != null || usage.output != null || usage.cacheRead != null || usage.cacheWrite != null;
  if (!hasCost && !hasTokens) {
    return null;
  }
  const model =
    assistant.provider && assistant.model ? `${assistant.provider}/${assistant.model}` : (assistant.model ?? null);
  return {
    source: "pi-coding/usage",
    type: "cost_snapshot",
    event_id: nextId(),
    timestamp: isoTimestamp(message),
    session_id: sessionId,
    cost: hasCost ? { total_cost_usd: cost } : null,
    tokens: hasTokens
      ? {
          input: usage.input ?? null,
          output: usage.output ?? null,
          cache_read: usage.cacheRead ?? null,
          cache_creation: usage.cacheWrite ?? null,
        }
      : null,
    model,
    cost_mode: "API_KEY",
  };
}

// Convert a pi AgentMessage into an mngr common-transcript record, or null for
// message roles the common schema does not represent (bashExecution, custom,
// branchSummary, compactionSummary). `nextId` is called at most once and only
// for emitted records, so the id counter stays dense.
export function toCommonRecord(
  message: AgentMessage,
  source: string,
  nextId: () => string,
): Record<string, unknown> | null {
  const timestamp = isoTimestamp(message);
  if (message.role === "user") {
    const user = message as UserMessage;
    return {
      timestamp,
      type: "user_message",
      event_id: nextId(),
      source,
      role: "user",
      content: textFromContent(user.content),
    };
  }
  if (message.role === "assistant") {
    const assistant = message as AssistantMessage;
    const usage = assistant.usage ?? {};
    return {
      timestamp,
      type: "assistant_message",
      event_id: nextId(),
      source,
      role: "assistant",
      model: assistant.model ?? "",
      text: textFromContent(assistant.content),
      tool_calls: toolCallsFromContent(assistant.content),
      parts: partsFromContent(assistant.content),
      parts_ordered: true,
      finish_reason: assistant.stopReason ?? "",
      usage: {
        input_tokens: usage.input ?? null,
        output_tokens: usage.output ?? null,
        cache_read_tokens: usage.cacheRead ?? null,
        cache_write_tokens: usage.cacheWrite ?? null,
      },
    };
  }
  if (message.role === "toolResult") {
    const result = message as ToolResultMessage;
    return {
      timestamp,
      type: "tool_result",
      event_id: nextId(),
      source,
      tool_call_id: result.toolCallId,
      tool_name: result.toolName,
      output: truncate(textFromContent(result.content), TOOL_OUTPUT_LIMIT),
      is_error: result.isError === true,
    };
  }
  return null;
}
