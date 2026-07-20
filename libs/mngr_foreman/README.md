# mngr_foreman

An always-on **web remote control for your mngr agents**. Run one Flask server on
a central box; from any device (including a phone) you get a live list of every
mngr agent, each claude agent's full transcript rendered as a terminal-style
view, and a composer to send messages. **No code is deployed to target boxes.**

It works because mngr already mirrors every claude agent's raw session JSONL,
verbatim and untruncated, to `<host_dir>/agents/<id>/logs/claude_transcript/events.jsonl`
on the agent's host (provisioned unconditionally by `mngr_claude`). Foreman just
tails that file remotely (via the same `HostFileReadInterface` mngr uses) and
parses it into diff-capable transcript events.

## Usage

```bash
mngr foreman serve --port 8700 --host 0.0.0.0
```

Then open `http://<box>:8700/` from any device on the network.

**There is no auth by design** (personal dev tool; the network is the boundary).
Bind to a tailnet IP (`--host 100.x.x.x`) or firewall the port. Do not expose it
to the public internet.

### Config (`[plugins.foreman]` in `settings.toml`)

- `port` (default `8700`)
- `host` (default `0.0.0.0`)
- `max_tool_output_chars` (default `20000`, `0` = unlimited) — cap on tool-result
  and non-diff tool-input length in the transcript.

CLI flags (`--port`, `--host`) override config.

Foreman shows **every agent in mngr's view** — chat (live transcript + send) for
`claude` agents, a web terminal for any type. Create agents with plain
`mngr create`; there is no foreman-specific create command or label filter.

## Status

- **Agent list, transcript view, send message.**
- **Web terminal** — `mngr connect` bridged over a pty↔websocket to xterm.js
  (`terminal.py`, `/a/<name>/terminal`, `WS /ws/agents/<name>/terminal`) for
  `/login`, permission prompts, and interrupts. `TMUX` is stripped from the child
  env; closing the tab detaches the tmux client without touching the agent.

## Notes / limitations

- Non-claude agents (codex, etc.) appear in the list but show a "no transcript
  for this agent type" notice — only claude agents mirror the raw JSONL.
- Subagent (Task/Agent) internals are not in the mirrored log; they appear only
  as the Task tool call and its result. Acceptable for a monitoring tool.
- A blocking TUI dialog on an agent (unanswered permission or `/login`) makes a
  send fail; the error is surfaced inline. Phase 2's terminal page is how you
  clear it.
- `marked` (markdown renderer) is vendored under `static/vendor/`; raw HTML in
  assistant output is escaped, not rendered.
