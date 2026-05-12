# imbue-mngr-claude-usage

Claude data provider for `mngr usage`. Single responsibility: install a tiny
statusline shim into each Claude agent so each render appends a rate-limit
event to `$MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl`. The
`mngr usage` CLI walks those events files itself (see `imbue-mngr-usage`).

## How the pieces fit

```
mngr_claude_usage's on_before_provisioning hookimpl
  └─→ writes <work_dir>/.claude/settings.local.json statusLine.command
  └─→ installs <state_dir>/commands/claude_statusline.sh (the shim)
  └─→ installs <state_dir>/commands/claude_rate_limits_writer.sh (the writer)
  └─→ captures any pre-existing user statusLine.command into
      <state_dir>/commands/user_statusline_cmd (the sidecar)

Claude Code statusline render (every turn)
  └─→ <work_dir>/.claude/settings.local.json's statusLine.command
        └─→ claude_statusline.sh
              ├─→ claude_rate_limits_writer.sh
              │     └─→ events/claude/rate_limits/events.jsonl (append one event)
              └─→ user's pre-existing statusLine.command (chain through, if any)

mngr usage
  └─→ list_agents (mngr core's CEL-filterable enumeration)
        └─→ for each matching agent: read events via the events API
              └─→ collapse to freshest snapshot per source; render
```

All file I/O goes through `host.read_text_file` / `host.write_file`, so the
provisioner works for local **and** remote agents (Modal, vps_docker, lima,
etc.).

## Caveat: multiple Pro/Max accounts share the `claude` source

`rate_limits` is emitted only by Anthropic.ai Pro/Max subscriptions
(direct-API-key / Bedrock / Vertex paths don't emit it -- they get filtered
naturally because no event is written). What's **not** filtered is the case
where multiple Pro/Max accounts contribute to the same `claude` source -- the
statusline payload has no per-account identifier, so `mngr usage` can't tell
"used 5h: 73%" from account A apart from "5h: 9%" from account B. The
collapse-to-freshest rule will silently mix them.

This is rare in practice (one user = one Anthropic account), but if you run
multiple Claude Code sessions logged into different Pro/Max accounts, treat
`mngr usage` as ambiguous: the "freshest" reading is for the last-rendered
account, not for any specific one. There's no field in the payload that would
let us label or warn from inside the writer; the only paths to resolution are
(a) capture auth-source from a different surface (e.g. Claude Code hooks
expose `apiKeySource` in their input -- not implemented here) or (b) shard the
source name per account via writer config (also not implemented).
