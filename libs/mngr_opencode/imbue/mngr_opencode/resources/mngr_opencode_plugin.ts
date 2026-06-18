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
//      the set is non-empty, so OpenCodeAgent.get_lifecycle_state can promote
//      RUNNING -> WAITING and `mngr list` can report a PERMISSIONS reason. Cleared
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
//      atomically. Rebuilding from full state on idle is robust (self-healing, no
//      message-completion detection) and needs no background converter/supervisor.
//      Once per turn is sufficient: the live in-progress view is the tmux pane
//      (mngr connect), and `mngr transcript` reads on demand. To survive
//      mngr stop/start (a fresh server with empty in-memory state, and opencode
//      does not replay history through the plugin), the state is seeded from the
//      persisted append-only raw transcript at startup, so the rebuild reflects
//      full history rather than truncating pre-restart turns.
//
// The root session id (for resume) is owned by mngr (opencode_launch.sh). Paths,
// the role/emit env vars, and the common `source` below are kept in sync with
// opencode_config.py (the Python side cannot be imported here). Every fs touch is
// wrapped so a transient error never disrupts OpenCode's loop.

import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"

// Keep in sync with opencode_config.py: ACTIVE_MARKER_FILENAME,
// PERMISSIONS_WAITING_FILENAME, RAW_TRANSCRIPT_RELATIVE_PATH,
// COMMON_TRANSCRIPT_RELATIVE_PATH, COMMON_TRANSCRIPT_SOURCE, ROLE_ENV_VAR,
// SERVER_ROLE, EMIT_COMMON_ENV_VAR.
const ACTIVE_MARKER_FILENAME = "active"
const PERMISSIONS_WAITING_FILENAME = "permissions_waiting"
const RAW_TRANSCRIPT_RELATIVE_PATH = "logs/opencode_transcript/events.jsonl"
const COMMON_TRANSCRIPT_RELATIVE_PATH = "events/opencode/common_transcript/events.jsonl"
const COMMON_TRANSCRIPT_SOURCE = "opencode/common_transcript"
const ROLE_ENV_VAR = "MNGR_OPENCODE_ROLE"
const SERVER_ROLE = "server"
const EMIT_COMMON_ENV_VAR = "MNGR_OPENCODE_EMIT_COMMON"

const _MAX_INPUT_PREVIEW_LENGTH = 200
const _MAX_OUTPUT_LENGTH = 2000

const _truncate = (text: string, limit: number): string => (text.length <= limit ? text : text.slice(0, limit) + "...")

const _shortValue = (value: unknown): string => (typeof value === "string" ? value : JSON.stringify(value))

const _isoFromMs = (createdMs: unknown): string =>
  typeof createdMs === "number" ? new Date(createdMs).toISOString().replace(/\.\d+Z$/, "Z") : ""

const _messageText = (parts: any[]): string =>
  parts
    .filter((part) => part?.type === "text" && !part?.synthetic && typeof part?.text === "string")
    .map((part) => part.text)
    .join("")

const _toolCallFromPart = (part: any): Record<string, unknown> => ({
  tool_call_id: part?.callID ?? "",
  tool_name: part?.tool ?? "",
  input_preview: _truncate(_shortValue(part?.state?.input ?? {}), _MAX_INPUT_PREVIEW_LENGTH),
})

const _toolStateOutput = (state: any): { output: string; isError: boolean } => {
  if (!state || typeof state !== "object") {
    return { output: "", isError: false }
  }
  if (state.status === "error") {
    return { output: _shortValue(state.error ?? ""), isError: true }
  }
  return { output: _shortValue(state.output ?? ""), isError: false }
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

  const buildCommonRecords = (): Record<string, unknown>[] => {
    const records: Record<string, unknown>[] = []
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
          timestamp,
          type: "user_message",
          event_id: message.id + "-user",
          source: COMMON_TRANSCRIPT_SOURCE,
          role: "user",
          content: text,
          conversation_id: sessionId,
          message_id: message.id,
        })
        continue
      }
      if (message?.role !== "assistant") {
        continue
      }

      const toolCalls = toolParts.map(_toolCallFromPart)
      // Build the ordered parts[] by walking the message's parts in arrival order, so the
      // text/tool_call interleaving is preserved (faithful -> parts_ordered: true).
      const commonParts: Array<Record<string, unknown>> = []
      for (const part of parts) {
        if (part?.type === "text" && !part?.synthetic && typeof part?.text === "string" && part.text) {
          commonParts.push({ type: "text", content: part.text })
        } else if (part?.type === "tool") {
          commonParts.push({ type: "tool_call", ..._toolCallFromPart(part) })
        }
      }
      const providerId = message?.providerID ?? ""
      const modelId = message?.modelID ?? ""
      records.push({
        timestamp,
        type: "assistant_message",
        event_id: message.id + "-assistant",
        source: COMMON_TRANSCRIPT_SOURCE,
        role: "assistant",
        model: providerId && modelId ? `${providerId}/${modelId}` : null,
        text,
        tool_calls: toolCalls,
        parts: commonParts,
        parts_ordered: true,
        finish_reason: message?.finish ?? null,
        usage: null,
        conversation_id: sessionId,
        message_id: message.id,
      })

      for (const part of toolParts) {
        const status = part?.state?.status
        if (status !== "completed" && status !== "error") {
          continue
        }
        const { output, isError } = _toolStateOutput(part?.state)
        records.push({
          timestamp,
          type: "tool_result",
          event_id: part.id + "-tool_result",
          source: COMMON_TRANSCRIPT_SOURCE,
          tool_call_id: part?.callID ?? "",
          tool_name: part?.tool ?? "",
          output: _truncate(output, _MAX_OUTPUT_LENGTH),
          is_error: isError,
          conversation_id: sessionId,
          message_id: part?.messageID ?? "",
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
      const tmpPath = commonTranscriptPath + ".tmp"
      writeFileSync(tmpPath, body.length > 0 ? body + "\n" : "")
      renameSync(tmpPath, commonTranscriptPath)
    } catch {
      // best-effort
    }
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
          if (isRootSession(event.properties.sessionID)) {
            clearMarkersForRootIdle()
          }
          rebuildCommon()
        }
        return
      }
      if (type === "session.idle") {
        if (isRootSession(event.properties.sessionID)) {
          clearMarkersForRootIdle()
        }
        rebuildCommon()
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
