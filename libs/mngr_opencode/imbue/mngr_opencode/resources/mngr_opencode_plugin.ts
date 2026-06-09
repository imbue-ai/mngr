// mngr lifecycle plugin for OpenCode agents.
//
// OpenCode has no POSIX-sh hook mechanism (unlike Claude Code / Antigravity);
// its blessed extension point is an in-process TypeScript plugin whose `event`
// hook receives every event-bus event. mngr drops this file into the per-agent
// OPENCODE_CONFIG_DIR/plugin/, where OpenCode auto-loads it.
//
// mngr runs the agent as a headless `opencode serve` process plus an
// `opencode attach` TUI client (see opencode_launch.sh), and BOTH load this
// plugin from the same config dir. The event hook fires server-side, but to
// avoid the attach client also acting (double-writing the marker/transcript)
// the plugin only does work when MNGR_OPENCODE_ROLE=server -- the role mngr
// sets exclusively on the serve invocation. In every other process it is inert.
//
// In the server process it does two things, keyed off $MNGR_AGENT_STATE_DIR:
//
//   1. Active marker -> RUNNING vs WAITING. BaseAgent.get_lifecycle_state reads
//      the presence of $MNGR_AGENT_STATE_DIR/active as "actively working". The
//      plugin touches it when a session goes busy and removes it when the ROOT
//      session goes idle. OpenCode reports status per session and the root
//      session stays busy for the whole turn -- including while task-tool
//      subagents (child sessions) run -- so gating the *clear* on the root
//      session (the one with no `parentID`) keeps the agent RUNNING until the
//      entire turn is done. Touching on any busy is safe; only clearing is
//      root-gated. A liveness fallback clears on idle when no session hierarchy
//      has been seen yet, so the marker can never strand.
//
//   2. Raw transcript. Each message.updated / message.part.updated event is
//      appended verbatim (as {type, properties}) to
//      $MNGR_AGENT_STATE_DIR/logs/opencode_transcript/events.jsonl. The Python
//      converter turns that into the common transcript `mngr transcript` reads.
//
// The root session id (for resume) is owned by mngr (opencode_launch.sh creates
// the session and records its id), NOT by this plugin. Filenames/paths and the
// role env below are kept in sync with opencode_config.py (the Python side
// cannot be imported here). Every fs touch is wrapped so a transient error
// never disrupts OpenCode's loop.

import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"

// Keep in sync with opencode_config.py: ACTIVE_MARKER_FILENAME,
// RAW_TRANSCRIPT_RELATIVE_PATH, ROLE_ENV_VAR, SERVER_ROLE.
const ACTIVE_MARKER_FILENAME = "active"
const RAW_TRANSCRIPT_RELATIVE_PATH = "logs/opencode_transcript/events.jsonl"
const ROLE_ENV_VAR = "MNGR_OPENCODE_ROLE"
const SERVER_ROLE = "server"

export const MngrLifecyclePlugin: Plugin = async () => {
  const stateDir = process.env.MNGR_AGENT_STATE_DIR
  // Only the mngr-managed server process maintains the marker/transcript. The
  // attach client (and any non-mngr run) loads this plugin too but stays inert,
  // so the marker and raw transcript have exactly one writer.
  if (!stateDir || process.env[ROLE_ENV_VAR] !== SERVER_ROLE) {
    return {}
  }

  const markerPath = join(stateDir, ACTIVE_MARKER_FILENAME)
  const rawTranscriptPath = join(stateDir, RAW_TRANSCRIPT_RELATIVE_PATH)

  // parentID per session id, learned from events that carry the full Session
  // (session.created / session.updated). Lets status/idle events -- which carry
  // only a sessionID -- be classified as root vs child without an async lookup.
  const parentBySession = new Map<string, string | undefined>()

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
    // Root = a session with no parent. Until we've seen this session's hierarchy
    // (via session.created/updated), fall back to treating it as root so idle can
    // still clear the marker rather than strand it.
    const parent = parentBySession.get(sessionId)
    return parent === undefined || parent === ""
  }

  return {
    event: async ({ event }) => {
      const type = event.type

      // Learn session hierarchy from events that carry the full Session.
      if (type === "session.created" || type === "session.updated") {
        const info = event.properties.info
        parentBySession.set(info.id, info.parentID)
        return
      }

      // Marker maintenance. session.status carries busy/idle/retry; session.idle
      // is the deprecated idle-only event (still emitted by older OpenCode) --
      // handle both for version tolerance.
      if (type === "session.status") {
        const status = event.properties.status.type
        if (status === "busy" || status === "retry") {
          touchMarker()
        } else if (status === "idle") {
          if (isRootSession(event.properties.sessionID)) {
            clearMarker()
          }
        }
        return
      }
      if (type === "session.idle") {
        if (isRootSession(event.properties.sessionID)) {
          clearMarker()
        }
        return
      }

      // Raw transcript: append message/part events verbatim.
      if (type === "message.updated" || type === "message.part.updated") {
        appendRaw(JSON.stringify({ type, properties: event.properties }))
        return
      }
    },
  }
}
