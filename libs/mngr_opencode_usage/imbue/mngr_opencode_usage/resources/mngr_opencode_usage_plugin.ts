// mngr usage writer plugin for OpenCode agents.
//
// OpenCode auto-loads every `$OPENCODE_CONFIG_DIR/plugin/*.ts`, so this file
// loads alongside mngr_opencode's lifecycle plugin without any config entry. Its
// single job: on each assistant `message.updated`, append one `cost_snapshot`
// event carrying that message's own cost + tokens to
//   $MNGR_AGENT_STATE_DIR/events/opencode/usage/events.jsonl
// which `mngr usage` reads (see imbue-mngr-usage).
//
// OpenCode reports cost/tokens PER MESSAGE (not cumulatively), so the reader uses
// the session-incremental strategy: it sums each session's messages, keeping the
// freshest event per `message_id` (so a streaming message's re-fires collapse to
// its final reading). That makes this writer stateless and restart-proof -- each
// event is self-contained and append-only; nothing is accumulated in memory.
//
// Like the lifecycle plugin, this only acts in the mngr-managed `opencode serve`
// process (MNGR_OPENCODE_ROLE=server); the attach client loads it too but stays
// inert, so usage events have exactly one writer. If MNGR_AGENT_STATE_DIR is
// unset (opencode run outside an mngr agent) it is a no-op. Every fs touch is
// wrapped so a transient error never disrupts OpenCode's loop.
//
// Reported cost is authoritative for OpenCode (provenance REPORTED); tokens are
// emitted for auditability. The model is provider-qualified (`<providerID>/
// <modelID>`) to match the canonical pricing-table key. cost_mode is API_KEY:
// OpenCode bills against a real provider key, not an imputed subscription.

import type { Plugin } from "@opencode-ai/plugin"
import { randomBytes } from "node:crypto"
import { appendFileSync, mkdirSync } from "node:fs"
import { dirname, join } from "node:path"

// Kept in sync with the reader's discovery convention (events/<source>/usage).
const USAGE_EVENTS_RELATIVE_PATH = "events/opencode/usage/events.jsonl"
const SOURCE = "opencode/usage"
const ROLE_ENV_VAR = "MNGR_OPENCODE_ROLE"
const SERVER_ROLE = "server"

const _numberOrNull = (value: unknown): number | null => (typeof value === "number" ? value : null)

export const MngrUsagePlugin: Plugin = async () => {
  const stateDir = process.env.MNGR_AGENT_STATE_DIR
  if (!stateDir || process.env[ROLE_ENV_VAR] !== SERVER_ROLE) {
    return {}
  }
  const usagePath = join(stateDir, USAGE_EVENTS_RELATIVE_PATH)

  let usageDirEnsured = false
  const appendUsage = (line: string): void => {
    try {
      if (!usageDirEnsured) {
        mkdirSync(dirname(usagePath), { recursive: true })
        usageDirEnsured = true
      }
      appendFileSync(usagePath, line + "\n")
    } catch {
      // best-effort: a transient fs error must not break OpenCode's loop
    }
  }

  return {
    event: async ({ event }) => {
      if (event.type !== "message.updated") {
        return
      }
      const message = (event.properties as any)?.info
      if (!message || message.role !== "assistant") {
        return
      }
      const sessionId = message.sessionID
      if (typeof sessionId !== "string" || !sessionId) {
        // session_id is contractually required by the reader; skip if absent.
        return
      }

      const cost = _numberOrNull(message.cost)
      const rawTokens = message.tokens
      const hasTokens = rawTokens && typeof rawTokens === "object"
      if (cost === null && !hasTokens) {
        // Nothing to report yet (e.g. an assistant message before any usage lands).
        return
      }

      // OpenCode token buckets are non-overlapping: input is non-cached, cache.read
      // / cache.write are separate. Reasoning is folded into output (the wire
      // convention: output includes reasoning, billed at the output rate). The
      // reported cost is authoritative regardless of this bucketing.
      const tokens = hasTokens
        ? {
            input: _numberOrNull(rawTokens.input),
            output: (rawTokens.output ?? 0) + (rawTokens.reasoning ?? 0),
            cache_read: _numberOrNull(rawTokens.cache?.read),
            cache_creation: _numberOrNull(rawTokens.cache?.write),
          }
        : null

      const model =
        typeof message.providerID === "string" && typeof message.modelID === "string"
          ? `${message.providerID}/${message.modelID}`
          : null

      const record = {
        source: SOURCE,
        type: "cost_snapshot",
        event_id: "evt-" + randomBytes(16).toString("hex"),
        timestamp: new Date().toISOString(),
        session_id: sessionId,
        message_id: typeof message.id === "string" ? message.id : null,
        cost: cost === null ? null : { total_cost_usd: cost },
        tokens,
        model,
        cost_mode: "API_KEY",
      }
      appendUsage(JSON.stringify(record))
    },
  }
}
