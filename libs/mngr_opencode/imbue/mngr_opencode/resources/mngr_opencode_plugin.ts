// mngr lifecycle plugin for OpenCode agents.
//
// OpenCode has no POSIX-sh hook mechanism (unlike Claude Code / Antigravity);
// its blessed extension point is an in-process TypeScript plugin whose `event`
// hook receives every event-bus event. mngr drops this file into the per-agent
// OPENCODE_CONFIG_DIR/plugin/, where OpenCode auto-loads it. It runs inside the
// OpenCode process (server-in-TUI), so it lives and dies with the agent.
//
// It does three things, all keyed off $MNGR_AGENT_STATE_DIR (which mngr exports
// onto the OpenCode process):
//
//   1. Active marker -> RUNNING vs WAITING. BaseAgent.get_lifecycle_state reads
//      the presence of $MNGR_AGENT_STATE_DIR/active as "actively working". This
//      plugin touches it when a session goes busy and removes it when the ROOT
//      session goes idle. OpenCode reports status per session, and the root
//      session stays busy for the whole turn -- including while subagents (child
//      sessions) it spawned via the task tool are running -- so gating the
//      *clear* on the root session id alone correctly keeps the agent RUNNING
//      until the entire turn (subagents included) is done. Touching on any
//      busy is safe; only clearing is root-gated. A liveness fallback clears on
//      idle when no root has been identified yet, so the marker can never strand.
//
//   2. Root session id for resume. The root session is the one with no
//      `parentID`. Its id is written to $MNGR_AGENT_STATE_DIR/<root-session-file>
//      so assemble_command can detect that a session exists and resume via
//      `opencode --continue` across stop/start (the recorded id itself is
//      informational). Subagents are child sessions and never overwrite it;
//      starting a fresh session (/new) updates it to the newest root.
//
//   3. Raw transcript. Each message.updated / message.part.updated event is
//      appended verbatim (as {type, properties}) to
//      $MNGR_AGENT_STATE_DIR/logs/opencode_transcript/events.jsonl. The Python
//      converter turns that into the common transcript `mngr transcript` reads.
//
// Filenames/relative paths below are kept in sync with opencode_config.py (the
// Python side cannot be imported here). Every fs touch is wrapped so a transient
// error never disrupts OpenCode's loop.

import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"

// Keep in sync with opencode_config.py: ACTIVE_MARKER_FILENAME,
// ROOT_SESSION_FILENAME, RAW_TRANSCRIPT_RELATIVE_PATH.
const ACTIVE_MARKER_FILENAME = "active"
const ROOT_SESSION_FILENAME = "opencode_root_session"
const RAW_TRANSCRIPT_RELATIVE_PATH = "logs/opencode_transcript/events.jsonl"

export const MngrLifecyclePlugin: Plugin = async () => {
  const stateDir = process.env.MNGR_AGENT_STATE_DIR
  // Outside an mngr-managed run there is nothing to maintain; stay inert rather
  // than crash OpenCode.
  if (!stateDir) {
    return {}
  }

  const markerPath = join(stateDir, ACTIVE_MARKER_FILENAME)
  const rootSessionPath = join(stateDir, ROOT_SESSION_FILENAME)
  const rawTranscriptPath = join(stateDir, RAW_TRANSCRIPT_RELATIVE_PATH)

  // parentID per session id, learned from events that carry the full Session
  // (session.created / session.updated). Lets status/idle events -- which carry
  // only a sessionID -- be classified as root vs child without an async lookup.
  const parentBySession = new Map<string, string | undefined>()
  let rootSessionId: string | null = null

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

  const recordRootSession = (sessionId: string): void => {
    if (rootSessionId === sessionId) {
      return
    }
    rootSessionId = sessionId
    try {
      writeFileSync(rootSessionPath, sessionId)
    } catch {
      // best-effort: resume simply falls back to a fresh session if unwritten
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
    if (rootSessionId !== null) {
      return sessionId === rootSessionId
    }
    // Root not yet identified: treat a session known to have no parent as root,
    // otherwise act as a liveness fallback (so idle can still clear the marker).
    const parent = parentBySession.get(sessionId)
    return parent === undefined || parent === ""
  }

  return {
    event: async ({ event }) => {
      const type = event.type

      // Learn session hierarchy from events that carry the full Session, and
      // record the root for resume.
      if (type === "session.created" || type === "session.updated") {
        const info = event.properties.info
        parentBySession.set(info.id, info.parentID)
        if (!info.parentID) {
          recordRootSession(info.id)
        }
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
