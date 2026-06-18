# imbue-mngr-codex-usage

Codex data provider for `mngr usage`. Codex exposes no statusline or in-process
plugin, but mngr already tails its rollout JSONL into a raw transcript via
mngr_codex's background-tasks supervisor. This package adds:

- **The writer** (`codex_usage.sh`): installed into the agent's `commands/` dir,
  where mngr_codex's `codex_background_tasks.sh` launches it **iff present**. It
  reads the raw rollout stream and emits one `cost_snapshot` per `token_count`
  item to `events/codex/usage/events.jsonl`.
- **The reader**: an `aggregate_usage_source` hookimpl claiming the `codex`
  source, aggregated **session-cumulatively** (each `token_count` carries the
  session's cumulative total, so the freshest per session wins).

## What gets captured

Codex reports cumulative token usage (`info.total_token_usage`) but **no dollar
cost**, so cost is left null and the reader **estimates** it from tokens via the
pricing table (provenance `ESTIMATED`). `input_tokens` includes cached, so the
writer emits `input = input_tokens - cached_input_tokens` and
`cache_read = cached_input_tokens` (OpenAI has no cache-write surcharge). The
model is `openai/<model>` from the rollout's `turn_context`.

In ChatGPT-plan (subscription) mode, `token_count` also carries 5h/7d rate-limit
windows, which the writer maps onto the window schema -- so Codex subscription
agents get Claude-style windows, and the session is classified `SUBSCRIPTION`
(imputed). Without rate limits it's `API_KEY` (real spend).
