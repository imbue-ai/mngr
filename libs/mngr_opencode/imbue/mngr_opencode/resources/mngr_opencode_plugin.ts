// mngr lifecycle + transcript plugin for OpenCode agents.
//
// OpenCode has no POSIX-sh hook mechanism (unlike Claude Code / Antigravity);
// its blessed extension point is an in-process TypeScript plugin whose `event`
// hook receives every event-bus event. mngr drops this file into the per-agent
// OPENCODE_CONFIG_DIR/plugin/, where OpenCode auto-loads it.
//
// mngr runs the agent as a headless `opencode serve` process plus an
// `opencode attach` TUI client (see opencode_launch.sh), and BOTH load this
// plugin from the same config dir. The event hook fires server-side, but to
// avoid the attach client also acting (double-writing) the plugin only does work
// when MNGR_OPENCODE_ROLE=server -- the role mngr sets exclusively on the serve
// invocation. In every other process it is inert.
//
// In the server process it does four things, keyed off $MNGR_AGENT_STATE_DIR:
//
//   1. Active marker -> RUNNING vs WAITING. BaseAgent.get_lifecycle_state reads
//      the presence of $MNGR_AGENT_STATE_DIR/active as "actively working". The
//      plugin touches it when a session goes busy and removes it when the ROOT
//      session goes idle (the session with no `parentID`), so task-tool subagents
//      keep the agent RUNNING until the whole turn is done.
//
//   1b. Permissions-waiting marker -> WAITING reason. While a tool is blocked on an
//      approval prompt (the `ask` permission policy), opencode emits
//      `permission.asked` (one per blocked tool, carrying the request id) and
//      `permission.replied` when it is answered. The plugin tracks the set of
//      pending ids and keeps $MNGR_AGENT_STATE_DIR/permissions_waiting present iff
//      the set is non-empty, so the agent's lifecycle reads WAITING and `mngr list`
//      reports a PERMISSIONS reason. Cleared
//      as a safety net on root idle (a prompt stranded without a reply). The marker
//      is independent of the active recompute -- the session stays busy (active
//      present) the whole time a prompt is open. (The running binary emits
//      `permission.asked` carrying `id`, and `permission.replied` carrying `requestID`
//      -- verified against 1.16.2 and 1.17.7. The @opencode-ai/sdk type stubs disagree,
//      naming them `permission.updated`/`permissionID`. The two handlers accept either,
//      since opencode self-upgrades.)
//
//   2. Raw transcript. Each message.updated / message.part.updated event is
//      appended verbatim (as {type, properties}) to
//      logs/opencode_transcript/events.jsonl.
//
//   3. Common transcript (when MNGR_OPENCODE_EMIT_COMMON=1). The plugin keeps the
//      latest message/part state in memory and, on session.idle, rebuilds the
//      agent-agnostic common transcript (events/opencode/common_transcript/
//      events.jsonl, what `mngr transcript` reads) from that state and writes it
//      atomically. Its records are the ATIF-shaped stream defined in
//      specs/atif-transcript-alignment/spec.md -- a `header` line followed by
//      `step` and `observation` records, at full fidelity (complete tool
//      arguments, untruncated outputs). Because the whole file is rewritten, the
//      header is simply the first record of every rebuild.
//      Rebuilding from full state on idle is robust (self-healing, no
//      message-completion detection) and needs no background converter/supervisor.
//      Once per turn is sufficient: the live in-progress view is the tmux pane
//      (mngr connect), and `mngr transcript` reads on demand. To survive
//      mngr stop/start (a fresh server with empty in-memory state, and opencode
//      does not replay history through the plugin), the state is seeded from the
//      persisted append-only raw transcript at startup, so the rebuild reflects
//      full history rather than truncating pre-restart turns.
//
// The root session id (for resume) is owned by mngr (opencode_launch.sh). Paths,
// the role/emit env vars, and the common `emitter` below are kept in sync with
// opencode_config.py (the Python side cannot be imported here). Every fs touch is
// wrapped so a transient error never disrupts OpenCode's loop.

import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import { appendFileSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import { basename, dirname, join } from "node:path"

// Keep in sync with opencode_config.py: ACTIVE_MARKER_FILENAME,
// PERMISSIONS_WAITING_FILENAME, RAW_TRANSCRIPT_RELATIVE_PATH,
// COMMON_TRANSCRIPT_RELATIVE_PATH, COMMON_TRANSCRIPT_EMITTER, ROLE_ENV_VAR,
// SERVER_ROLE, EMIT_COMMON_ENV_VAR.
const ACTIVE_MARKER_FILENAME = "active"
const PERMISSIONS_WAITING_FILENAME = "permissions_waiting"
const RAW_TRANSCRIPT_RELATIVE_PATH = "logs/opencode_transcript/events.jsonl"
const COMMON_TRANSCRIPT_RELATIVE_PATH = "events/opencode/common_transcript/events.jsonl"
const COMMON_TRANSCRIPT_EMITTER = "opencode/common_transcript"
const ROLE_ENV_VAR = "MNGR_OPENCODE_ROLE"
const SERVER_ROLE = "server"
const EMIT_COMMON_ENV_VAR = "MNGR_OPENCODE_EMIT_COMMON"

// The ATIF revision the emitted records follow. Kept in sync with
// PINNED_ATIF_SCHEMA_VERSION in imbue/mngr/agents/common_transcript_records.py.
const ATIF_SCHEMA_VERSION = "ATIF-v1.7"

const IMAGE_PLACEHOLDER = "[image omitted]"

const _asText = (value: unknown): string => (typeof value === "string" ? value : (JSON.stringify(value) ?? ""))

// A step timestamp must parse as ISO 8601, so a message with no usable
// `time.created` gets the epoch rather than an empty string -- and the epoch rather
// than "now" so a rebuild does not rewrite the record with a different timestamp.
const _isoFromMs = (createdMs: unknown): string =>
  new Date(typeof createdMs === "number" ? createdMs : 0).toISOString().replace(/\.\d+Z$/, "Z")

// ATIF requires `arguments` to be a JSON object. A native value that is not one is
// preserved verbatim under `_raw` rather than dropped.
const _argumentsObject = (value: unknown): Record<string, unknown> => {
  if (value === null || value === undefined) {
    return {}
  }
  if (typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  if (typeof value === "string") {
    // An absent or empty native payload means "no arguments", not a raw empty string.
    if (!value.trim()) {
      return {}
    }
    try {
      const parsed = JSON.parse(value)
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
    } catch {
      // not JSON; fall through to the _raw wrapper
    }
    return { _raw: value }
  }
  return { _raw: _asText(value) }
}

// The image file-part shape read here is asserted by synthetic fixtures, not confirmed
// against captured native opencode output.
const _isImagePart = (part: any): boolean =>
  part?.type === "file" && typeof part?.mime === "string" && part.mime.startsWith("image/")

const _messageText = (parts: any[]): string =>
  parts
    .map((part) => {
      if (part?.type === "text" && !part?.synthetic && typeof part?.text === "string") {
        return part.text
      }
      return _isImagePart(part) ? IMAGE_PLACEHOLDER : ""
    })
    .join("")

// Thinking, where opencode surfaces it. Several reasoning parts in one inference
// are joined with blank lines (the spec's reasoning_content rule). The reasoning
// part shape read here is asserted by synthetic fixtures, not confirmed against
// captured native opencode output.
const _reasoningText = (parts: any[]): string =>
  parts
    .filter((part) => part?.type === "reasoning" && typeof part?.text === "string" && part.text)
    .map((part) => part.text)
    .join("\n\n")

const _toolCallFromPart = (part: any): Record<string, unknown> => ({
  tool_call_id: part?.callID ?? "",
  function_name: part?.tool ?? "",
  arguments: _argumentsObject(part?.state?.input),
})

const _toolStateOutput = (state: any): { output: string; isError: boolean } => {
  if (!state || typeof state !== "object") {
    return { output: "", isError: false }
  }
  if (state.status === "error") {
    return { output: _asText(state.error ?? ""), isError: true }
  }
  return { output: _asText(state.output ?? ""), isError: false }
}

export const MngrLifecyclePlugin: Plugin = async () => {
  const stateDir = process.env.MNGR_AGENT_STATE_DIR
  // Only the mngr-managed server process maintains the marker/transcripts. The
  // attach client (and any non-mngr run) loads this plugin too but stays inert,
  // so the marker and transcripts have exactly one writer.
  if (!stateDir || process.env[ROLE_ENV_VAR] !== SERVER_ROLE) {
    return {}
  }

  const markerPath = join(stateDir, ACTIVE_MARKER_FILENAME)
  const permissionsWaitingPath = join(stateDir, PERMISSIONS_WAITING_FILENAME)
  const rawTranscriptPath = join(stateDir, RAW_TRANSCRIPT_RELATIVE_PATH)
  const commonTranscriptPath = join(stateDir, COMMON_TRANSCRIPT_RELATIVE_PATH)
  const emitCommon = process.env[EMIT_COMMON_ENV_VAR] === "1"

  // parentID per session id, learned from session.created/updated (which carry
  // the full Session). Lets status/idle events -- which carry only a sessionID --
  // be classified root vs child without an async lookup.
  const parentBySession = new Map<string, string | undefined>()
  // Latest message info / parts per id, for rebuilding the common transcript.
  const messageById = new Map<string, any>()
  const partsByMessage = new Map<string, Map<string, any>>()

  // Fold a message.updated / message.part.updated event into the in-memory state
  // the common transcript is rebuilt from. Used both for live events and to seed
  // from the persisted raw transcript at startup (below). Keyed by id, so it is
  // idempotent -- replaying the same event (or a later update of a streaming part)
  // is last-write-wins.
  const accumulateMessageEvent = (eventType: string, properties: any): void => {
    if (eventType === "message.updated") {
      messageById.set(properties.info.id, properties.info)
    } else if (eventType === "message.part.updated") {
      const part = properties.part
      const parts = partsByMessage.get(part.messageID) ?? new Map<string, any>()
      parts.set(part.id, part)
      partsByMessage.set(part.messageID, parts)
    }
  }

  // Seed the maps from the persisted raw transcript so the first post-restart
  // rebuild reflects FULL history. opencode does NOT replay historical events
  // through the plugin on `attach --session` resume (verified), and rebuildCommon
  // does a full atomic overwrite -- so without this, the first idle after
  // `mngr start` would truncate every pre-restart turn from the common transcript
  // (the raw transcript is append-only and would survive, an asymmetric loss).
  // The raw JSONL is exactly the event log we need; replaying it here is
  // idempotent with any later live updates (keyed by id).
  if (emitCommon) {
    try {
      const persisted = readFileSync(rawTranscriptPath, "utf8")
      for (const line of persisted.split("\n")) {
        if (!line.trim()) {
          continue
        }
        try {
          const seedEvent = JSON.parse(line)
          accumulateMessageEvent(seedEvent.type, seedEvent.properties)
        } catch {
          // skip a malformed/partial line
        }
      }
    } catch {
      // no persisted raw transcript yet (first start)
    }
  }

  const touchMarker = (): void => {
    try {
      writeFileSync(markerPath, "")
    } catch {
      // best-effort: a transient fs error must not break OpenCode's loop
    }
  }

  const clearMarker = (): void => {
    try {
      rmSync(markerPath, { force: true })
    } catch {
      // best-effort
    }
  }

  // Permission ids currently awaiting a reply. The permissions_waiting marker is
  // present iff this set is non-empty. The binary emits one `permission.asked` per
  // tool blocked on approval (carrying the request `id`) and one `permission.replied`
  // (carrying `requestID`) when it is answered; the handlers also accept the SDK stub
  // aliases (see the header and the two handlers below). Tracking ids (rather than a
  // single flag like codex) handles concurrent prompts, e.g. from task-tool subagents.
  const pendingPermissions = new Set<string>()

  const refreshPermissionsMarker = (): void => {
    try {
      if (pendingPermissions.size > 0) {
        writeFileSync(permissionsWaitingPath, "")
      } else {
        rmSync(permissionsWaitingPath, { force: true })
      }
    } catch {
      // best-effort: a transient fs error must not break OpenCode's loop
    }
  }

  // Clear the active marker AND any pending permission state when the root turn
  // ends. The permissions reset is a safety net: a prompt cancelled/denied or
  // stranded without a `permission.replied` would otherwise leave the agent
  // reporting PERMISSIONS forever. The whole turn is done, so nothing can still be
  // legitimately pending.
  const clearMarkersForRootIdle = (): void => {
    clearMarker()
    pendingPermissions.clear()
    refreshPermissionsMarker()
  }

  // Clear any stranded permissions_waiting marker at server startup. The in-memory
  // pendingPermissions set is the authority within a server's lifetime (the marker
  // is derived from it), and a freshly started server has none pending -- so an
  // on-disk marker here is stale, left by a prior server that was killed/crashed
  // mid-prompt (a clean turn-end clears it via clearMarkersForRootIdle). Without
  // this, after `mngr stop`/`start` a stale marker would falsely read PERMISSIONS
  // once the next turn sets `active`. This is opencode's analog of codex clearing a
  // stranded marker at a fresh root turn (and of claude's startup reset).
  refreshPermissionsMarker()

  let rawDirEnsured = false
  const appendRaw = (line: string): void => {
    try {
      if (!rawDirEnsured) {
        mkdirSync(dirname(rawTranscriptPath), { recursive: true })
        rawDirEnsured = true
      }
      appendFileSync(rawTranscriptPath, line + "\n")
    } catch {
      // best-effort
    }
  }

  const isRootSession = (sessionId: string): boolean => {
    // Root = a session with no parent. Until we've seen this session's hierarchy,
    // fall back to treating it as root so idle can still clear the marker.
    const parent = parentBySession.get(sessionId)
    return parent === undefined || parent === ""
  }

  // The ATIF-shaped stream: a header line, one `step` record per message, and one
  // `observation` record per finished tool call (see
  // specs/atif-transcript-alignment/spec.md). Per-agent annotations live under the
  // ATIF `extra` objects -- the record schema forbids unknown top-level fields.
  const buildCommonRecords = (): Record<string, unknown>[] => {
    // The header id hashes the agent id and emitter: a fixed "header" id repeats
    // identically across the fleet, so analytics' event-id dedupe would collapse
    // every agent's header to one.
    const headerDigest = createHash("sha256")
      .update(`${basename(stateDir)}:${COMMON_TRANSCRIPT_EMITTER}`)
      .digest("hex")
      .slice(0, 32)
    const records: Record<string, unknown>[] = [
      {
        type: "header",
        event_id: `header-${headerDigest}`,
        emitter: COMMON_TRANSCRIPT_EMITTER,
        schema_version: ATIF_SCHEMA_VERSION,
      },
    ]
    const messages = [...messageById.values()].sort(
      (a, b) => (a?.time?.created ?? 0) - (b?.time?.created ?? 0),
    )
    for (const message of messages) {
      const parts = [...(partsByMessage.get(message.id)?.values() ?? [])]
      const timestamp = _isoFromMs(message?.time?.created)
      const sessionId = message?.sessionID ?? ""
      const text = _messageText(parts)
      const toolParts = parts.filter((part) => part?.type === "tool")

      if (message?.role === "user") {
        if (!text) {
          continue
        }
        records.push({
          type: "step",
          event_id: `${message.id}-user`,
          emitter: COMMON_TRANSCRIPT_EMITTER,
          timestamp,
          source: "user",
          message: text,
          extra: { conversation_id: sessionId, message_id: message.id },
        })
        continue
      }
      if (message?.role !== "assistant") {
        continue
      }

      const providerId = message?.providerID ?? ""
      const modelId = message?.modelID ?? ""
      const reasoning = _reasoningText(parts)
      const finishReason = message?.finish
      const extra: Record<string, unknown> = { conversation_id: sessionId, message_id: message.id }
      if (finishReason) {
        // ATIF has no stop-reason field, so it rides as a step-level extra.
        extra.finish_reason = finishReason
      }
      const step: Record<string, unknown> = {
        type: "step",
        event_id: `${message.id}-assistant`,
        emitter: COMMON_TRANSCRIPT_EMITTER,
        timestamp,
        // One agent step per assistant message: ATIF has no interleaving concept, so
        // the message text is concatenated and the tool calls are an ordered list.
        source: "agent",
        message: text,
        extra,
      }
      if (providerId && modelId) {
        step.model_name = `${providerId}/${modelId}`
      }
      if (reasoning) {
        step.reasoning_content = reasoning
      }
      if (toolParts.length > 0) {
        step.tool_calls = toolParts.map(_toolCallFromPart)
      }
      // opencode reports no per-message token usage, so the step carries no metrics.
      records.push(step)

      for (const part of toolParts) {
        const status = part?.state?.status
        if (status !== "completed" && status !== "error") {
          continue
        }
        const { output, isError } = _toolStateOutput(part?.state)
        records.push({
          type: "observation",
          event_id: `${part.id}-tool_result`,
          emitter: COMMON_TRANSCRIPT_EMITTER,
          timestamp,
          results: [
            {
              source_call_id: part?.callID ?? "",
              content: output,
              // is_error / tool_name have no ATIF field of their own; the ids repeat the
              // step's provenance so a result stands on its own.
              extra: {
                conversation_id: sessionId,
                message_id: message.id,
                is_error: isError,
                tool_name: part?.tool ?? "",
              },
            },
          ],
        })
      }
    }
    return records
  }

  const rebuildCommon = (): void => {
    if (!emitCommon) {
      return
    }
    try {
      mkdirSync(dirname(commonTranscriptPath), { recursive: true })
      const body = buildCommonRecords()
        .map((record) => JSON.stringify(record))
        .join("\n")
      const tmpPath = `${commonTranscriptPath}.tmp`
      // The header is always the first record, so the body is never empty.
      writeFileSync(tmpPath, body + "\n")
      renameSync(tmpPath, commonTranscriptPath)
    } catch {
      // best-effort
    }
  }

  // A session finished its turn. Newer opencode reports this as a `session.status`
  // carrying an idle status; older builds emit the now-deprecated standalone
  // `session.idle`. opencode self-upgrades, so both arms route here.
  const handleTurnIdle = (sessionId: string): void => {
    if (isRootSession(sessionId)) {
      clearMarkersForRootIdle()
    }
    rebuildCommon()
  }

  return {
    event: async ({ event }) => {
      const type = event.type

      if (type === "session.created" || type === "session.updated") {
        const info = event.properties.info
        parentBySession.set(info.id, info.parentID)
        return
      }

      if (type === "session.status") {
        const status = event.properties.status.type
        if (status === "busy" || status === "retry") {
          touchMarker()
        } else if (status === "idle") {
          handleTurnIdle(event.properties.sessionID)
        }
        return
      }
      if (type === "session.idle") {
        handleTurnIdle(event.properties.sessionID)
        return
      }

      // A tool is blocked on an approval prompt. The running opencode server
      // (verified against the 1.16.2 binary) emits `permission.asked`; the
      // `@opencode-ai/sdk` type stubs instead name it `permission.updated`. The two
      // disagree, and opencode self-upgrades, so accept either -- both carry the
      // request id in `properties.id`.
      if (type === "permission.asked" || type === "permission.updated") {
        pendingPermissions.add(event.properties.id)
        refreshPermissionsMarker()
        return
      }
      // The prompt was answered (allowed or denied). The reply references the asked
      // request id; the running binary names it `requestID`, the sdk stubs name it
      // `permissionID` -- accept either.
      if (type === "permission.replied") {
        pendingPermissions.delete(event.properties.requestID ?? event.properties.permissionID)
        refreshPermissionsMarker()
        return
      }

      if (type === "message.updated" || type === "message.part.updated") {
        accumulateMessageEvent(type, event.properties)
        appendRaw(JSON.stringify({ type, properties: event.properties }))
        return
      }
    },
  }
}
