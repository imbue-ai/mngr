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
//   4. Model/effort state. pi carries no static per-agent model config file (its
//      model comes from launch args / pi settings, its effort from the thinking
//      level), so the chat model bar cannot read the selection off disk. Instead
//      this extension writes the harness-uniform
//      `$MNGR_AGENT_STATE_DIR/model_state.json`
//      (`{model: "provider/id", effort, fast}` -- the schema every harness's
//      writer emits; see docs/system/blueprint/live-model-state/ in the
//      workspace template) from the pi-resolved values: on `session_start`
//      (which fires at TUI startup, before the first prompt, so the pre-turn-1
//      selection is available immediately) and again on `model_select` /
//      `thinking_level_select` as the user switches. The system interface reads
//      this file for the model bar.
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

import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
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
interface PiModel {
  provider?: string;
  id?: string;
}
// pi's model registry, the synchronous facade pi exposes to extensions on the ctx.
// ``find`` resolves a provider + model id to pi's own Model object (the object
// ``setModel`` wants); ``hasConfiguredAuth`` reports whether that model's provider is
// authenticated. Optional so a stub/fake ctx (the test harness) still type-checks.
interface ModelRegistry {
  find?: (provider: string, modelId: string) => PiModel | undefined;
  hasConfiguredAuth?: (model: PiModel) => boolean;
}

// Minimal structural view of pi's editor (composer) accessors, exposed on ctx.ui.
// Used by the atomic shoulder-tap: on abort pi drains its parked steers INTO the
// editor, so we read them back and restore any pre-existing draft. Optional so the
// test's fake ctx still type-checks; every use is guarded on presence.
interface ExtensionUi {
  getEditorText?: () => string;
  setEditorText?: (text: string) => void;
}

interface ExtensionContext {
  sessionManager?: SessionManager;
  // Active model and its current effective thinking level (pi's effort axis). pi
  // passes these on the ctx to every handler; read to record the live model/effort
  // for the chat model bar.
  model?: PiModel;
  thinkingLevel?: string;
  // pi's model registry (see above); used to resolve a control-file switch's
  // "provider/model" slug into the Model object ``pi.setModel`` requires.
  modelRegistry?: ModelRegistry;
  // Whether the agent is idle (not streaming). Used to gate the atomic shoulder-tap
  // (only interrupt a running turn) and to know when it is safe to resubmit.
  isIdle?: () => boolean;
  // Abort the current agent operation. In interactive mode (how mngr runs pi) this
  // routes to pi's own handler, which drains the parked steers into the editor and
  // stops the model stream -- both synchronously.
  abort?: () => void;
  // Editor (composer) accessors; see ExtensionUi.
  ui?: ExtensionUi;
}

// pi's ExtensionAPI -- `on`, `sendUserMessage` (input injection without tmux
// keystrokes), and the native model/effort setters `setModel` / `setThinkingLevel`.
// All but `on` are optional so a stub/fake `pi` (the test harness) still type-checks;
// each caller guards on presence. `setModel` returns false when the provider is unauthed.
interface PiApi {
  on: (event: string, handler: (event: any, ctx: ExtensionContext) => void | Promise<void>) => void;
  sendUserMessage?: (content: string, options?: { deliverAs?: "steer" | "followUp" }) => void | Promise<void>;
  setModel?: (model: PiModel) => Promise<boolean> | boolean;
  setThinkingLevel?: (level: string) => void;
}

// --- Constants kept in sync with plugin.py / base_agent.py. -----------------

const ACTIVE_MARKER_NAME = "active";
const SESSION_STARTED_SENTINEL_NAME = "pi_session_started";
const SESSION_FILE_NAME = "pi_session_file";
// The live model state in the harness-uniform schema ({model, effort, fast}),
// written for the chat model bar to read before turn 1 and across switches.
// Kept in sync with the shared reader on the system-interface side
// (harnesses/model.py).
const MODEL_STATE_NAME = "model_state.json";
// mngr appends one JSON-encoded message string per line here; we inject each new
// line into the live session via pi.sendUserMessage (no tmux keystrokes). Kept
// in sync with _INBOX_FILE_NAME in plugin.py.
const INBOX_NAME = "pi_inbox";
// Where prior-generation inbox lines are archived at load (raw history preserved
// verbatim), so `pi_inbox` itself only ever holds current-generation lines. The
// Minds queue mirror replays `pi_inbox` from zero and relies on this scoping.
const INBOX_HISTORY_NAME = "pi_inbox_history";
const INBOX_POLL_MS = 200;
// How many consecutive drain ticks a flush/retract sentinel may be deferred waiting for
// injected steers to park (pendingInjections to drain) before it is consumed anyway:
// 10 ticks at the 200ms cadence is ~2s -- the same bound as the dwt stop path's message-lock
// wait (STOP_LOCK_WAIT_SECONDS = 2.0) and far above a normal park (sub-second). The bound
// keeps stop-wins (U2): a send whose promise never settles must not defer the abort forever
// (an unstoppable turn). Proceeding past the bound means a still-un-parked steer can escape
// the flush/retract and commit as a visible duplicate -- the class the queue-sweep series
// already accepts -- which is strictly better than a stop that never lands.
const SENTINEL_SETTLE_MAX_TICKS = 10;
// Atomic shoulder-tap control records. Minds appends one JSON OBJECT line to pi_inbox
// (a normal message is a JSON *string*, so the two never collide). Because it rides the
// same ordered append-only inbox, every message queued before it has already been injected
// by the time we see it. There are two sentinels, one distinct key each -- a separate key,
// not a field on one, so an old extension treats the unknown key as inert rather than
// mistaking a retract for a flush and double-sending. Kept in sync with the Minds pi
// endpoint (harnesses/pi_coding/inbox.py).
//   * Flush (shoulder tap): interrupt the running turn and RESUBMIT its parked steers as
//     one merged turn.
const INTERRUPT_KEY = "minds_interrupt";
//   * Retract (stop button): interrupt the running turn and DISCARD its parked steers --
//     Minds hands the queued messages back to the user's composer, so resubmitting them
//     here would double-deliver.
const RETRACT_KEY = "minds_interrupt_retract";
// Single-slot switch mailbox: the chat model bar's resolver atomically
// OVERWRITES this file with one JSON intent ({model_id: "provider/model",
// thinking_level: "high"|null}) -- a newer pick replaces an unconsumed older
// one (buffer of size 1, last wins). We consume it (rename, apply, delete),
// so anything present at startup is by definition pending: a switch parked
// while the agent was stopped applies on the next start. Kept in sync with
// _CONTROL_NAME in the pi harness resolver (harnesses/pi_coding/model.py).
const CONTROL_NAME = "pi_control.json";
const CONTROL_POLL_MS = 200;

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

// --- Shell-command safety guards (see system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/tool-call-policies.md). -------
//
// Rules that hold for every pi agent, applied in the `tool_call` handler: return
// `{block, reason}` to refuse a command, or mutate `event.input.command` to
// rewrite it. A rule that belongs to the repo an agent runs in goes in that
// repo's own `.pi/extensions/`, which pi loads alongside this one.

// Block: a command that pipes into tail/head.
const PIPE_TAIL_HEAD_RE = /\|\s*(tail|head)(\s|$)/;
// Block: git history-rewriting commands.
const GIT_REBASE_RE = /^git\s+rebase/;
const GIT_COMMIT_RE = /^git\s+commit\b/;
const GIT_COMMIT_REWRITE_RE = /--(amend|fixup)/;
const GIT_PULL_RE = /^git\s+pull\b/;
const GIT_PULL_REBASE_RE = /(--rebase|\s-r(\s|$))/;
// The OOM self-tag band for agent subprocesses (kept in sync with
// oom_priority.bands.AGENT_SUBPROCESS == 900).
const OOM_SUBPROCESS_BAND = 900;

/** Single-quote a value for safe interpolation into a shell command. */
function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

/** The reason to block a command, or null if it is allowed. Cannot throw.
 *
 * These strings are copied VERBATIM from system/scripts/agent_block_pipe_tail_head.sh and
 * agent_prevent_commit_rewrite.sh, which claude/codex/agy run directly. pi has no shell-hook
 * surface, so it re-expresses the rules here -- and the tool-call policy contract requires a
 * harness that reproduces rather than executes to copy the wording verbatim, so an agent gets
 * an identical explanation on every harness. Change the scripts and this together.
 */
function commandBlockReason(command: string): string | null {
  if (PIPE_TAIL_HEAD_RE.test(command)) {
    return (
      "Do not pipe commands through tail or head. Instead, redirect output to a temp file " +
      "(e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool " +
      "or a separate tail/head command on the file."
    );
  }
  if (GIT_REBASE_RE.test(command)) return "Blocked: git rebase commands are not allowed";
  if (GIT_COMMIT_RE.test(command) && GIT_COMMIT_REWRITE_RE.test(command)) {
    return "Blocked: git commit with --amend or --fixup is not allowed";
  }
  if (GIT_PULL_RE.test(command) && GIT_PULL_REBASE_RE.test(command)) {
    return "Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)";
  }
  return null;
}

/** Read a string field from a mngr data.json, or null. */
function readDataField(path: string, field: string): string | null {
  try {
    const value = (JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>)[field];
    return typeof value === "string" && value ? value : null;
  } catch {
    return null;
  }
}

/** The `export GIT_AUTHOR_.../GIT_COMMITTER_...; ` prefix, or "" when unresolved.
 * Name from <state_dir>/data.json (fallback MNGR_AGENT_NAME), email
 * <agent_id>@<host_id>. */
function gitIdentityPrefix(): string {
  const agentId = process.env.MNGR_AGENT_ID;
  const stateDir = process.env.MNGR_AGENT_STATE_DIR;
  const hostDir = process.env.MNGR_HOST_DIR;
  const name =
    (stateDir ? readDataField(join(stateDir, "data.json"), "name") : null) ??
    process.env.MNGR_AGENT_NAME ??
    null;
  const hostId = hostDir ? readDataField(join(hostDir, "data.json"), "host_id") : null;
  if (!name || !agentId || !hostId) return "";
  const email = `${agentId}@${hostId}`;
  const q = shellQuote;
  return (
    `export GIT_AUTHOR_NAME=${q(name)} GIT_COMMITTER_NAME=${q(name)} ` +
    `GIT_AUTHOR_EMAIL=${q(email)} GIT_COMMITTER_EMAIL=${q(email)}; `
  );
}

/** The guarded oom self-tag prefix (mirrors build_oom_tag_prefix). */
function oomTagPrefix(): string {
  return `test -w /proc/self/oom_score_adj && echo ${OOM_SUBPROCESS_BAND} > /proc/self/oom_score_adj 2>/dev/null; `;
}

/** Prepend the git-identity (if resolvable) + oom-tag prefixes to a command. */
function rewriteBashCommand(command: string): string {
  return gitIdentityPrefix() + oomTagPrefix() + command;
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
  const modelStatePath = join(stateDir, MODEL_STATE_NAME);
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

  // Record the live model + thinking level (pi's effort axis) for the chat model
  // bar, in the harness-uniform schema: {model: "provider/id", effort, fast}.
  // Called on session_start -- which fires at TUI startup, before the first
  // prompt -- so the pre-turn-1 selection is available immediately, and on
  // model_select / thinking_level_select as the user switches. The changed axis is
  // taken from the event (ctx.model / ctx.thinkingLevel may not have updated yet at
  // the instant the event fires); the untouched axis comes from ctx. Nothing is
  // written until the model resolves, so a prior good state is never blanked by a
  // thinking-only event. Atomic (fixed tmp name + rename) so the reader never
  // sees a torn write.
  const recordModelState = (ctx: ExtensionContext, override?: { model?: PiModel; thinkingLevel?: string }): void => {
    const model = override?.model ?? ctx.model;
    const thinkingLevel = override?.thinkingLevel ?? ctx.thinkingLevel;
    const provider = typeof model?.provider === "string" ? model.provider : "";
    const modelId = typeof model?.id === "string" ? model.id : "";
    const thinking = typeof thinkingLevel === "string" ? thinkingLevel : "";
    if (!provider || !modelId) {
      return;
    }
    const tmpPath = modelStatePath + ".tmp";
    writeFileSync(tmpPath, JSON.stringify({ model: `${provider}/${modelId}`, effort: thinking || null, fast: false }));
    renameSync(tmpPath, modelStatePath);
  };

  // Input injection. mngr delivers messages by appending one JSON-encoded string
  // per line to <state>/pi_inbox; we inject each new line via pi.sendUserMessage
  // so the agent receives input without tmux keystroke simulation, while the TUI
  // stays viewable. At load (before session_start writes the readiness sentinel
  // mngr waits on) the prior generation's lines are archived to pi_inbox_history
  // and the inbox is truncated, so a resumed restart never re-injects the prior
  // session's already-delivered messages, and -- because mngr only writes after
  // seeing the sentinel -- no message sent right after readiness is skipped.
  //
  // Delivered as `steer`, not `followUp`: pi's agent loop re-polls its steering queue
  // after every tool-call round and injects steered messages before the next model
  // response, so a message sent to a busy agent reaches it greedily at the next tool
  // boundary rather than waiting for the whole turn to end (followUp). Sent to an idle
  // agent, steer starts a turn the same as followUp would.
  // The latest ExtensionContext, captured in the handlers below. Held here so the
  // inbox/control drain timers can reach ctx.abort()/isIdle()/ui and modelRegistry.
  // pi's ctx getters read live runner state, so a held ctx stays valid.
  let latestCtx: ExtensionContext | undefined;

  const inboxPath = join(stateDir, INBOX_NAME);
  // Generation-scope the durable inbox: any lines present at load were written for a
  // prior process generation (mngr only appends after the readiness sentinel, which
  // `session_start` writes AFTER this load, so nothing can land concurrently here).
  // Archive them to the sibling history file (raw history preserved verbatim) and
  // truncate the inbox in place, so the inbox holds current-generation lines only BY
  // CONSTRUCTION -- the offset seed below then reads 0, and the Minds queue mirror's
  // replay-from-zero of `pi_inbox` is generation-scoped with no extra bookkeeping.
  safe("inbox generation reset", () => {
    if (existsSync(inboxPath)) {
      const prior = readFileSync(inboxPath, "utf-8");
      if (prior.length > 0) {
        appendFileSync(join(stateDir, INBOX_HISTORY_NAME), prior);
        writeFileSync(inboxPath, "");
      }
    }
  });
  let processedInbox = countLines(inboxPath);
  // Holds the parked-steer text between an atomic shoulder-tap's abort and its
  // resubmit. While non-null the inbox drain is PAUSED (no new steer is injected), so
  // nothing can open a competing turn between the interrupt and the resubmit.
  let pendingResubmit: string | null = null;

  // Count of steer injections whose async send has been initiated but not yet settled (parked).
  // A sentinel (flush/retract) must not abort until this is 0: pi.sendUserMessage resolves when
  // the message LANDS in the steering queue, so an abort while injections are still outstanding
  // could fire before a just-injected steer has parked, letting it escape the flush/retract and
  // commit as a stray turn. Gating the sentinel on this counter makes "the steer has parked"
  // provable rather than assumed-within-one-poll (the prior single-tick deferral). The gate is
  // BOUNDED by SENTINEL_SETTLE_MAX_TICKS so a send that never settles cannot defer a stop forever.
  let pendingInjections = 0;
  // Consecutive drain ticks the current head-of-inbox sentinel has been deferred by the settle
  // gate; reset whenever a sentinel is consumed.
  let sentinelDeferredTicks = 0;

  const injectSteer = (content: string): void => {
    // Delivery is best-effort. pi.sendUserMessage is async (returns a Promise), so the
    // offset advances right after the call is initiated, not after the message lands --
    // an async rejection is logged and not retried. We must attach a rejection handler:
    // a bare `void promise` would surface as an unhandled rejection, which on modern
    // Node terminates the process and would take pi down with it (the one thing this
    // extension must never do). We also track the in-flight count (settled in `finally`)
    // so a sentinel can wait for the steer to actually park -- see `pendingInjections`.
    const sent = pi.sendUserMessage?.(content, { deliverAs: "steer" });
    if (sent != null && typeof (sent as Promise<void>).then === "function") {
      pendingInjections++;
      // Assimilate through Promise.resolve: the guard above proves only `.then`, so a
      // then-only thenable would lack `.catch`/`.finally` and the chain would throw
      // synchronously, leaking the pendingInjections entry and deadlocking the settle gate.
      Promise.resolve(sent as Promise<void>)
        .catch((error) => logDiagnostic("inbox inject", error))
        .finally(() => {
          pendingInjections--;
        });
    }
  };

  // The shared abort-and-capture core of both sentinels. If a turn is running, interrupt it
  // and return its parked steers; returns null when idle (nothing to interrupt). Runs in ONE
  // synchronous tick -- no await between the isIdle check and the abort -- so nothing
  // (agent_end, the agent loop) can interleave; this is the anti-race guarantee.
  //
  // pi's abort (in interactive mode) drains the parked steers INTO the composer and stops the
  // stream, both synchronously. Since it APPENDS onto whatever is typed, we clear any draft
  // first and restore it after -- so the captured steers are sourced purely from pi's own
  // queue (authoritative; no Minds-view lag) and no draft leaks in. The two sentinel branches
  // differ ONLY in the returned steers' fate: flush resubmits them, retract discards them.
  const abortAndCaptureSteers = (): string | null => {
    const ctx = latestCtx;
    if (!ctx || typeof ctx.isIdle !== "function" || ctx.isIdle() || typeof ctx.abort !== "function") {
      return null; // idle / no live turn -> nothing to interrupt
    }
    const draft = ctx.ui?.getEditorText?.() ?? "";
    ctx.ui?.setEditorText?.("");
    ctx.abort();
    const steers = ctx.ui?.getEditorText?.() ?? "";
    ctx.ui?.setEditorText?.(draft);
    return steers;
  };

  const drainInbox = (): void => {
    safe("inbox", () => {
      // Waiting to resubmit after an interrupt: hold ALL injection until the aborted
      // turn settles (isIdle), then send the captured steers as one merged fresh turn.
      if (pendingResubmit !== null) {
        const ctx = latestCtx;
        if (ctx && typeof ctx.isIdle === "function" && ctx.isIdle()) {
          const text = pendingResubmit;
          pendingResubmit = null;
          if (text.trim() !== "") {
            injectSteer(text);
          }
        }
        return;
      }
      if (typeof pi.sendUserMessage !== "function" || !existsSync(inboxPath)) {
        return;
      }
      const lines = readFileSync(inboxPath, "utf-8").split("\n");
      const total = lines[lines.length - 1] === "" ? lines.length - 1 : lines.length;
      // Whether this tick has already injected a string line. injectSteer initiates an ASYNC
      // send, so a sentinel encountered after one must be DEFERRED (below) until every injected
      // steer has parked -- otherwise the abort could fire before a just-injected steer has
      // landed in the steering queue, and the steer would escape the flush/retract.
      let injectedStringThisTick = false;
      while (processedInbox < total) {
        const raw = lines[processedInbox];
        if (raw === "") {
          processedInbox++;
          continue;
        }
        let content: unknown;
        try {
          content = JSON.parse(raw);
        } catch {
          // Skip a malformed line rather than inject garbage or stall.
          processedInbox++;
          continue;
        }
        if (typeof content === "string") {
          processedInbox++;
          injectSteer(content);
          injectedStringThisTick = true;
          continue;
        }
        const isFlush = content !== null && typeof content === "object" && (content as Record<string, unknown>)[INTERRUPT_KEY] === true;
        const isRetract = content !== null && typeof content === "object" && (content as Record<string, unknown>)[RETRACT_KEY] === true;
        if (isFlush || isRetract) {
          // Deferral: never consume a sentinel while any injected steer is still un-parked --
          // either one was injected in THIS tick, or an injection from a prior tick has not yet
          // settled (pendingInjections > 0). Return WITHOUT advancing processedInbox so this
          // sentinel is re-read on a later tick, once every prior steer has landed in the
          // steering queue and is thus flushable/retractable. Waiting on the actual settle
          // (not a single poll) is what makes the abort provably not race a slow-parking steer.
          // The deferral is BOUNDED (SENTINEL_SETTLE_MAX_TICKS): past the bound the sentinel
          // proceeds even with injections outstanding, so a send whose promise never settles
          // cannot make the turn unstoppable -- the un-parked steer may then escape as a
          // visible duplicate, the accepted trade for a stop that always lands.
          if ((injectedStringThisTick || pendingInjections > 0) && sentinelDeferredTicks < SENTINEL_SETTLE_MAX_TICKS) {
            sentinelDeferredTicks++;
            return;
          }
          sentinelDeferredTicks = 0;
          processedInbox++;
          // Every prior message rode the same ordered inbox, so it is already injected.
          const steers = abortAndCaptureSteers();
          if (steers === null) {
            // Agent was idle, nothing to interrupt -> keep draining.
            continue;
          }
          if (isFlush) {
            // Flush: resubmit the parked steers as one merged turn (pause the drain until idle).
            pendingResubmit = steers;
          }
          // Retract: discard the steers -- Minds hands them back to the user's composer, so
          // resubmitting them here would double-deliver. pendingResubmit stays null, so the
          // drain simply resumes on the next tick.
          return; // interrupted -> end this tick
        }
        // An unknown object line (a foreign/future sentinel) is inert under skew: skip it.
        processedInbox++;
      }
    });
  };
  const inboxTimer = setInterval(drainInbox, INBOX_POLL_MS);
  if (typeof inboxTimer.unref === "function") {
    inboxTimer.unref();
  }

  // Model/effort switching from the chat model bar: a single-slot mailbox (see
  // CONTROL_NAME). We apply the intent natively via pi.setModel /
  // pi.setThinkingLevel; applying fires model_select / thinking_level_select,
  // whose handlers below write model_state.json, so the bar reconciles
  // from the live state file with no extra wiring.
  //
  // Model resolution needs ctx.modelRegistry, which pi hands to event handlers, not to this
  // timer -- so we hold the latest ctx (captured in the handlers). The registry's getters read
  // live runner state, so a held ctx stays valid. Nothing is consumed until a
  // ctx exists, so a switch parked before session_start (including while the
  // agent was stopped) is applied exactly once, never dropped.
  const controlPath = join(stateDir, CONTROL_NAME);
  const controlConsumePath = controlPath + ".consuming";

  const applySwitch = (intent: unknown, ctx: ExtensionContext): void => {
    if (intent == null || typeof intent !== "object") {
      return;
    }
    const record = intent as { model_id?: unknown; thinking_level?: unknown };
    const modelId = typeof record.model_id === "string" ? record.model_id : "";
    const thinkingLevel = typeof record.thinking_level === "string" ? record.thinking_level : "";
    // Effort: pi's ThinkingLevel strings are exactly our effort strings, so pass through.
    // Skip when unchanged to avoid a redundant thinking_level_select.
    if (thinkingLevel && ctx.thinkingLevel !== thinkingLevel && typeof pi.setThinkingLevel === "function") {
      try {
        pi.setThinkingLevel(thinkingLevel);
      } catch (error) {
        logDiagnostic("switch: setThinkingLevel", error);
      }
    }
    // Model: resolve the "provider/model" slug to pi's Model via the registry, then apply if
    // authed and not already current. Model ids can contain "/", so split on the first only.
    if (modelId) {
      const slash = modelId.indexOf("/");
      const provider = slash > 0 ? modelId.slice(0, slash) : "";
      const id = slash > 0 ? modelId.slice(slash + 1) : "";
      const registry = ctx.modelRegistry;
      if (!provider || !id || typeof registry?.find !== "function") {
        logDiagnostic("switch: unresolved model id", modelId);
      } else {
        const model = registry.find(provider, id);
        if (model == null) {
          logDiagnostic("switch: unknown model", modelId);
        } else if (typeof registry.hasConfiguredAuth === "function" && !registry.hasConfiguredAuth(model)) {
          logDiagnostic("switch: provider not authenticated", provider);
        } else {
          const alreadyCurrent = ctx.model?.provider === provider && ctx.model?.id === id;
          if (!alreadyCurrent && typeof pi.setModel === "function") {
            const applied = pi.setModel(model);
            if (applied != null && typeof (applied as Promise<boolean>).catch === "function") {
              (applied as Promise<boolean>).catch((error) => logDiagnostic("switch: setModel", error));
            }
          }
        }
      }
    }
  };

  const drainControl = (): void => {
    safe("control", () => {
      const ctx = latestCtx;
      if (ctx === undefined || !existsSync(controlPath)) {
        // No ctx yet (before session_start): leave the mailbox so the intent
        // applies once ctx exists.
        return;
      }
      // Consume by rename: the resolver's atomic overwrite either lands before
      // the rename (we apply it) or after (it creates a fresh mailbox for the
      // next drain). Reading the renamed copy means a concurrent overwrite can
      // never be half-read or silently deleted unapplied.
      renameSync(controlPath, controlConsumePath);
      const raw = readFileSync(controlConsumePath, "utf-8");
      rmSync(controlConsumePath, { force: true });
      let intent: unknown = null;
      try {
        intent = JSON.parse(raw);
      } catch {
        // Malformed mailbox: drop it rather than stall (the next pick rewrites it).
      }
      if (intent !== null) {
        applySwitch(intent, ctx);
      }
    });
  };
  const controlTimer = setInterval(drainControl, CONTROL_POLL_MS);
  if (typeof controlTimer.unref === "function") {
    controlTimer.unref();
  }

  // Also covers /new, /resume and fork: pi fires session_start for each with
  // `reason: "new" | "resume" | "fork"`, so recording here is enough. A separate
  // `session_switch` handler used to sit below this one; pi declares no such event and emits
  // it nowhere, so it never ran -- and `pi.on()` does not validate event names, so a wrong
  // one is a silent no-op rather than an error.
  pi.on("session_start", (_event, ctx) => {
    safe("session_start", () => {
      // Hold the ctx so the control-file drain can reach ctx.modelRegistry (see drainControl).
      latestCtx = ctx;
      // Session file and model state land BEFORE the readiness sentinel: the
      // sentinel is the signal mngr's create wait reports readiness on, and
      // everything the chat surface needs at first paint (the model bar reads
      // model_state.json) must already be on disk when that signal fires.
      recordSessionFile(ctx);
      recordModelState(ctx);
      mkdirSync(dirname(sentinelPath), { recursive: true });
      writeFileSync(sentinelPath, "1");
    });
  });

  // pi's model + thinking-level (effort) selectors. model_select fires on the
  // /model command, Ctrl+P cycling, or session restore; thinking_level_select on any
  // thinking change. Recording both keeps model_state.json live so the chat model
  // bar reconciles to a terminal-side switch.
  pi.on("model_select", (event, ctx) => {
    safe("model_select", () => {
      latestCtx = ctx;
      recordModelState(ctx, { model: event?.model });
    });
  });

  pi.on("thinking_level_select", (event, ctx) => {
    safe("thinking_level_select", () => {
      latestCtx = ctx;
      recordModelState(ctx, { thinkingLevel: event?.level });
    });
  });

  pi.on("agent_start", (_event, _ctx) => {
    safe("agent_start", () => {
      writeFileSync(markerPath, "1");
    });
  });

  // Policy guard: block disallowed bash commands and rewrite the rest with the
  // oom self-tag + git identity (see the "Policy guards" section above and
  // system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/tool-call-policies.md). NOT wrapped in safe() -- a guard that
  // swallowed its error would fail OPEN. The block check is a pure regex over a
  // string and cannot throw; the best-effort rewrite is isolated so a failure
  // leaves the command unchanged rather than blocking a legitimate command.
  // `event`/return are loosely typed here to match PiApi.on's shim signature; at
  // runtime pi passes a BashToolCallEvent with a mutable `input` and honors a
  // returned `{block, reason}` (see the SDK's ToolCallEvent/ToolCallEventResult).
  pi.on("tool_call", (event: any) => {
    if (event?.toolName !== "bash") return;
    const input = event.input as { command?: string };
    const command = input?.command;
    if (typeof command !== "string" || !command) return;
    const reason = commandBlockReason(command);
    if (reason !== null) return { block: true, reason };
    // Record the agent's own command BEFORE rewriting it, in its own try.
    //
    // The rewrite prepends `export ...; test -w ...; `, and pi calls every extension's
    // tool_call handler on this same event -- and the project extensions load AFTER this one
    // (CLI `-e` paths come first in resource-loader's mergePaths), so `.pi/extensions/`
    // guards always read a command this handler has already rewritten. They therefore rely on
    // `mngrOriginalCommand`; without it they see the prefix as a command chained ahead of the
    // agent's and refuse EVERY permission request and EVERY `tk start`/`tk close`.
    //
    // These were one try block, recording after the rewrite so "a frozen event cannot cost the
    // rewrite itself". That trade is the wrong way round: if the event ever became partly
    // frozen, the rewrite would land, the recording would throw, the catch would swallow it,
    // and the guards would then block every request -- fail-closed-wrong. Losing the rewrite
    // only costs an OOM band; losing the recording breaks the guards.
    try {
      event.mngrOriginalCommand = command;
    } catch {
      // Nothing to do: the guards fall back to `input.command`, which is still unrewritten here.
    }
    try {
      input.command = rewriteBashCommand(command);
    } catch {
      // Rewrite is best-effort (matches claude's pass-through-on-failure); never block on it.
    }
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
