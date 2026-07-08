# imbue-mngr-codex-usage

Codex data provider for `mngr usage`. Codex exposes no statusline or in-process
plugin, so this package reads usage from the rollout stream that mngr_codex
already tails, and aggregates it for `mngr usage` (see `imbue-mngr-usage`).

## What gets captured

Codex reports cumulative token usage but no dollar cost, so cost is estimated
from tokens via the pricing table (provenance `ESTIMATED`). The model is
`openai/<model>` from the rollout's `turn_context`.

In ChatGPT-plan (subscription) mode, Codex also reports 5h/7d rate-limit windows,
so those agents are classified `SUBSCRIPTION` (imputed). Without rate limits the
session is `API_KEY` (real spend).
