Added `specs/agent-usage-plugins/spec.md`: a design spec for extending `mngr usage` cost/usage tracking beyond Claude to the OpenCode, pi, and Codex harnesses. The spec generalizes the usage event schema to report raw token counts (with the reader deriving and provenance-flagging cost via a canonical pricing table), keeps dollars as the cross-harness comparable unit, and lays out three thin per-harness writer plugins. Antigravity and the Claude-subagent-proxy are documented as out of scope. The
per-harness data exposure was verified against the locally installed harnesses
(OpenCode 1.16.2, Codex 0.138.0, pi 0.79.1): OpenCode reports cost+tokens
directly; Codex's `token_count` events expose cumulative tokens plus rate-limit
windows (so Codex subscription agents get Claude-style windows as a bonus). A
live two-turn `pi-coding` agent confirmed pi reports cost natively
(`usage.cost.total`, matching the canonical Anthropic prices exactly) with
non-overlapping cache-exclusive token buckets, so pi is reported-cost (estimate
only as a fallback), leaving Codex as the only purely token-derived harness.
