# imbue-mngr-claude-usage

Claude data provider for `mngr usage`. Single responsibility: install a tiny
statusline shim into each Claude agent so each render appends one event to
`$MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl`. The event
carries three things from the Claude Code statusline payload: `rate_limits`
(Pro/Max only), `cost` (always), and `session_id` (always). The
`mngr usage` CLI walks those events files itself (see `imbue-mngr-usage`).

## How the pieces fit

```
mngr_claude_usage's on_before_provisioning hookimpl
  └─→ writes <work_dir>/.claude/settings.local.json statusLine.command
  └─→ installs <state_dir>/commands/claude_statusline.sh (the shim)
  └─→ installs <state_dir>/commands/claude_usage_writer.sh (the writer)
  └─→ captures any pre-existing user statusLine.command into
      <state_dir>/commands/user_statusline_cmd (the sidecar)

Claude Code statusline render (every turn)
  └─→ <work_dir>/.claude/settings.local.json's statusLine.command
        └─→ claude_statusline.sh
              ├─→ claude_usage_writer.sh
              │     └─→ events/claude/usage/events.jsonl (append one event)
              └─→ user's pre-existing statusLine.command (chain through, if any)

mngr usage
  └─→ list_agents (mngr core's CEL-filterable enumeration)
        └─→ for each matching agent: scan events, aggregate by (source, session_id),
            render per-session costs + freshest rate limits
```

All file I/O goes through `host.read_text_file` / `host.write_file`, so the
provisioner works for local **and** remote agents (Modal, vps_docker, lima,
etc.).

## What gets captured under each auth mode

The Claude Code statusline payload's contents depend on how the user is
authenticated:

| Field         | Pro/Max subscription                                | API key (ANTHROPIC_API_KEY) |
| ------------- | --------------------------------------------------- | --------------------------- |
| `rate_limits` | Present after the first API response of the session | Not emitted at all          |
| `cost`        | Present                                             | Present                     |
| `session_id`  | Present                                             | Present                     |

So `mngr usage` shows rate-limit windows only for subscribers, but
cost-per-session works under both auth modes. The writer emits one event
whenever **either** `rate_limits` or `cost` is present -- so an API-key
user still gets cost tracking. The reader uses the presence/absence of
`rate_limits` to classify each Claude Code process as `SUBSCRIPTION`
(cost is imputed by Claude Code, not billable) or `API_KEY` (cost is
real billable spend), and exposes the two aggregates separately:
`api_cost.total_cost_usd > 5.0` predicates real spend (the case here),
while `subscription_cost.total_cost_usd > 50.0` predicates imputed
value-received under a flat subscription.

`session_id` is carried alongside cost to anchor the cost reading to a
specific Claude Code session. Cost resets per session, so a delta across
snapshots is only meaningful within one `session_id`.

## Caveat: multiple Pro/Max accounts share the `claude` source

What's **not** filtered is the case where multiple Pro/Max accounts
contribute to the same `claude` source -- the statusline payload has no
per-account identifier, so `mngr usage` can't tell "used 5h: 73%" from
account A apart from "5h: 9%" from account B. The aggregation rules will
silently mix them:

- Rate-limit windows are reduced freshest-wins per source, so the displayed
  five_hour / seven_day reading is for whichever account rendered most
  recently -- not any specific one.
- Cost is grouped per-session (and per-(agent, Claude Code process)
  upstream), so per-session records in `sessions[]` stay correct
  individually. But the per-mode aggregates `subscription_cost.*` /
  `api_cost.*` (and the human `subscription cost (imputed):` /
  `api cost:` lines) sum across every session in the recency window of
  the corresponding mode regardless of account, and `sessions[0]` is
  just whichever session last rendered. Subscription accounts and
  API-key accounts stay distinguishable in the split (the auth mode is
  detected per process), but two Pro/Max accounts both contributing
  to `subscription_cost` are not.

This is rare in practice (one user = one Anthropic account), but if you run
multiple Claude Code sessions logged into different Pro/Max accounts, treat
the aggregated `mngr usage` view as ambiguous across accounts. There's no
field in the payload that would let us label or warn from inside the
writer; the only paths to resolution are (a) capture auth-source from a
different surface (e.g. Claude Code hooks expose `apiKeySource` in their
input -- not implemented here) or (b) shard the source name per account
via writer config (also not implemented).
