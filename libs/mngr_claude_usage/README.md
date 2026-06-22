# imbue-mngr-claude-usage

Claude data provider for `mngr usage`. It installs a tiny statusline shim into
each Claude agent so every render records usage. The `mngr usage` CLI reads and
aggregates that data (see `imbue-mngr-usage`).

## What gets captured under each auth mode

What the Claude Code statusline reports depends on how the user is authenticated:

| Field         | Pro/Max subscription                                | API key (ANTHROPIC_API_KEY) |
| ------------- | --------------------------------------------------- | --------------------------- |
| `rate_limits` | Present after the first API response of the session | Not emitted at all          |
| `cost`        | Present                                             | Present                     |
| `session_id`  | Present                                             | Present                     |

So `mngr usage` shows rate-limit windows only for subscribers, but
cost-per-session works under both auth modes. The presence/absence of
`rate_limits` classifies each Claude Code process as `SUBSCRIPTION` (cost is
imputed by Claude Code, not billable) or `API_KEY` (real billable spend), and the
two aggregates are exposed separately: `api_cost.total_cost_usd > 5.0` predicates
real spend, while `subscription_cost.total_cost_usd > 50.0` predicates imputed
value received under a flat subscription.

## Caveat: multiple Pro/Max accounts share the `claude` source

If multiple Pro/Max accounts contribute to the same `claude` source, `mngr usage`
cannot tell them apart -- the statusline payload has no per-account identifier.
The rate-limit reading reflects whichever account rendered most recently, and the
`subscription_cost.*` aggregate sums across accounts. Per-session records in
`sessions[]` stay correct individually, and subscription vs. API-key modes stay
distinguishable, but two Pro/Max accounts cannot be separated.

If you run multiple
Claude Code sessions logged into different Pro/Max accounts, treat the aggregated
`mngr usage` view as ambiguous across accounts.
