# imbue-mngr-opencode-usage

OpenCode data provider for `mngr usage`. Single responsibility: install a second
in-process TypeScript plugin into each OpenCode agent so each assistant message
appends one event to `$MNGR_AGENT_STATE_DIR/events/opencode/usage/events.jsonl`.
The `mngr usage` CLI walks those events files itself (see `imbue-mngr-usage`).

## How the pieces fit

```
mngr_opencode_usage's on_after_provisioning hookimpl
  └─→ installs <config_dir>/plugin/mngr_opencode_usage_plugin.ts
        (alongside mngr_opencode's lifecycle plugin; OpenCode auto-loads plugin/*.ts)

OpenCode serve process, on each assistant message.updated
  └─→ mngr_opencode_usage_plugin.ts
        └─→ events/opencode/usage/events.jsonl (append one cost_snapshot event)

mngr usage
  └─→ aggregate_usage_source hookimpl (claims the "opencode" source)
        └─→ aggregate_session_incremental: sum each session's messages
```

All file I/O goes through `host.write_text_file`, so provisioning works for local
**and** remote agents.

## What gets captured

OpenCode computes cost and tokens per message and exposes them on each assistant
`message.updated` event. The writer emits, per message, a self-contained event
carrying:

- `cost.total_cost_usd` -- OpenCode's own per-message cost (provenance is
  **REPORTED**; no estimation needed).
- `tokens` -- `{input, output, cache_read, cache_creation}` (reasoning folded
  into output), for auditability.
- `model` -- the provider-qualified `<providerID>/<modelID>`.
- `session_id` + `message_id` -- the reader sums per session, keeping the freshest
  event per `message_id` so a streaming message's re-fires collapse to its final
  reading.

## Why session-incremental

OpenCode reports each message's **own** cost, not a session running total. So the
writer is stateless -- it appends one self-contained event per message and keeps
nothing in memory. The reader (`aggregate_session_incremental`) recomputes each
session's total from the append-only log, which makes it robust across
`mngr stop`/`start` (a fresh serve process with empty memory loses no history).

`cost_mode` is `API_KEY`: OpenCode bills against a real provider key, so the cost
is real spend, never an imputed subscription value.
