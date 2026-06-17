# imbue-mngr-pi-coding-usage

pi data provider for `mngr usage`. pi loads a single explicit extension
(mngr_pi_coding's lifecycle extension, via `pi -e`), so the per-message usage
**writer** lives there -- it already holds each assistant message's cost and
tokens. This package owns the two usage-specific pieces:

- **The reader**: an `aggregate_usage_source` hookimpl claiming the `pi-coding`
  source, aggregated session-incrementally (pi reports cost per message, summed
  per session).
- **The writer gate**: `on_after_provisioning` drops a `pi_emit_usage` marker in
  each pi agent's state dir. The lifecycle extension only emits usage events when
  that marker is present -- so the events are written exactly when their reader
  (this package) is installed.

## What gets captured

pi computes per-message cost client-side (`usage.cost.total`), so cost is
**REPORTED** (no estimation). Each assistant `message_end` appends one
`cost_snapshot` to `events/pi-coding/usage/events.jsonl` with the reported cost,
tokens (`input`/`output`/`cacheRead`/`cacheWrite`), the provider-qualified model
(`<provider>/<model>`), and the session id (the pi session file's basename).
`cost_mode` is `API_KEY` (pi bills a real provider key).

## Why the writer is in the harness extension

Unlike Claude (statusline shim) or OpenCode (auto-loaded `plugin/*.ts`), pi loads
exactly one explicit extension owned by mngr_pi_coding. A standalone writer would
require a harness change either way, so the writer is folded into the lifecycle
extension (which already extracts the message usage), and this package stays the
reader + gate.
