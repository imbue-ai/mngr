# imbue-mngr-opencode-usage

OpenCode data provider for `mngr usage`. It installs an in-process plugin into
each OpenCode agent so every assistant message records usage. The `mngr usage`
CLI reads and aggregates that data (see `imbue-mngr-usage`).

## What gets captured

OpenCode computes cost and tokens per message. Each message records:

- `cost.total_cost_usd` -- OpenCode's own per-message cost (provenance
  **REPORTED**; no estimation needed).
- `tokens` -- `{input, output, cache_read, cache_creation}` (reasoning folded
  into output), for auditability.
- `model` -- the provider-qualified `<providerID>/<modelID>`.
- `session_id` + `message_id` -- the reader sums per session, keeping the
  freshest event per message so a streaming message's re-fires collapse to its
  final reading.

`cost_mode` is `API_KEY`: OpenCode bills against a real provider key, so the cost
is real spend, never an imputed subscription value.
