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
//      touches it on `agent_start` and removes it on `agent_end`. To stay
//      correct when a *nested* pi (spawned by the bash tool, inheriting
//      `MNGR_AGENT_STATE_DIR` and `PI_CODING_AGENT_DIR`) runs its own
//      agent_start/agent_end against the same marker, we record the root turn's
//      session id and only clear for that root -- the same root-vs-child
//      discriminator mngr_antigravity uses for agy subagents. (pi has no
//      in-process subagent/Task tool, so a nested process is the only child
//      case.)
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
import { dirname, join } from "node:path";

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
  getSessionId?: () => string | undefined;
  getSessionFile?: () => string | undefined;
}
interface ExtensionContext {
  sessionManager?: SessionManager;
}

// pi's ExtensionAPI -- only the `on` method is used here.
interface PiApi {
  on: (event: string, handler: (event: any, ctx: ExtensionContext) => void | Promise<void>) => void;
}

// --- Constants kept in sync with plugin.py / base_agent.py. -----------------

const ACTIVE_MARKER_NAME = "active";
const SESSION_STARTED_SENTINEL_NAME = "pi_session_started";
const ROOT_SESSION_NAME = "pi_root_session";
const SESSION_FILE_NAME = "pi_session_file";

const INPUT_PREVIEW_LIMIT = 200;
const TOOL_OUTPUT_LIMIT = 2000;

// --- Helpers. ---------------------------------------------------------------

function safe(label: string, fn: () => void): void {
  try {
    fn();
  } catch (error) {
    // Never let a lifecycle/transcript failure disrupt pi. Best-effort log to
    // stderr only; pi treats extension stderr as diagnostic, not as agent input.
    try {
      process.stderr.write(`[mngr_pi_lifecycle] ${label} failed: ${String(error)}\n`);
    } catch {
      // Give up silently -- nothing we can safely do here.
    }
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
  const rootSessionPath = join(stateDir, ROOT_SESSION_NAME);
  const sessionFilePath = join(stateDir, SESSION_FILE_NAME);
  const rawPath = join(stateDir, "logs", `${agentType}_transcript`, "events.jsonl");
  const commonPath = join(stateDir, "events", agentType, "common_transcript", "events.jsonl");
  const commonSource = `${agentType}/common_transcript`;

  // event_id must be unique within commonPath so `mngr transcript`'s dedupe set
  // never drops a real record. Seed the counter from the existing line count so
  // ids keep climbing across stop/start (a `--continue` restart reuses the same
  // session id but only fires message_end for *new* messages, so a per-session
  // reset would collide with ids written before the restart).
  let commonSeq = emitCommon ? countLines(commonPath) : 0;

  const readRootSession = (): string => {
    try {
      return existsSync(rootSessionPath) ? readFileSync(rootSessionPath, "utf-8").trim() : "";
    } catch {
      return "";
    }
  };

  const currentSessionId = (ctx: ExtensionContext): string => {
    try {
      return ctx.sessionManager?.getSessionId?.() ?? "";
    } catch {
      return "";
    }
  };

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

  pi.on("agent_start", (_event, ctx) => {
    safe("agent_start", () => {
      // Record the root only at a turn boundary (marker absent), so a nested pi
      // that starts mid-turn does not overwrite the true root with its own id.
      if (!existsSync(markerPath)) {
        const sessionId = currentSessionId(ctx);
        if (sessionId) {
          writeFileSync(rootSessionPath, sessionId);
        }
      }
      writeFileSync(markerPath, "1");
    });
  });

  pi.on("agent_end", (_event, ctx) => {
    safe("agent_end", () => {
      const root = readRootSession();
      const sessionId = currentSessionId(ctx);
      // Clear for the root turn, or -- liveness fallback -- when no root was
      // ever recorded, so a failure to capture the id can't strand RUNNING.
      if (root === "" || root === sessionId) {
        rmSync(markerPath, { force: true });
      }
    });
  });

  pi.on("session_shutdown", (_event, _ctx) => {
    safe("session_shutdown", () => {
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
      stop_reason: assistant.stopReason ?? "",
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
