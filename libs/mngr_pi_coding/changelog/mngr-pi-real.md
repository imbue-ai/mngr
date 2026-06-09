Brought the `pi-coding` agent type up to real lifecycle parity with the mature
agent plugins. The plugin now provisions a single mngr-owned pi extension (loaded
with `pi -e`) that drives everything pi has no shell hooks for:

- `mngr list` now reports RUNNING vs WAITING for pi agents (an `active` marker
  maintained on pi's `agent_start`/`agent_end` events), and stays correct when an
  agent spawns a nested `pi` via its bash tool.
- `mngr transcript <agent>` now works for pi agents, and a raw pi message stream is
  captured under the agent state dir. New config: `emit_common_transcript`,
  `emit_raw_transcript` (both default on).
- `mngr stop` then `mngr start` now resumes the same pi session with full context.
  New config: `resume_session` (default on).
- Agent creation now waits on a real readiness signal (a sentinel the extension
  writes when pi's session loads) rather than only scraping the startup banner.
- Auto-install now uses the current npm package `@earendil-works/pi-coding-agent`
  (the old `@mariozechner/pi-coding-agent` scope is deprecated and frozen).
- Also sync the `agents/` resource dir from `~/.pi/agent/` into each agent's
  config dir (alongside skills/prompts/extensions/themes), so an installed
  subagent extension finds its agent definitions (pi has no built-in subagents).
  The `npm` dir is deliberately *not* synced: pi auto-installs the `packages`
  listed in the synced `settings.json` into each agent's `$PI_CODING_AGENT_DIR/npm`
  on startup, so npm-package extensions (e.g. `npm:pi-subagents`) are available
  without copying `node_modules`, at the cost of a ~1s per-agent install that
  needs network on first launch.
- Deliver messages by injecting them into the live session via the lifecycle
  extension (`pi.sendUserMessage`) rather than simulating tmux keystrokes: mngr
  appends each message to a per-agent inbox file and the extension's watcher
  injects it. The TUI stays viewable (attach with `mngr connect`), and delivery
  is more reliable than the old paste+Enter path (pi intermittently swallowed the
  first Enter) and behaves identically on local and remote hosts.
- Handle pi 0.79+'s "Trust project folder?" dialog: mngr pre-trusts the agent's
  workspace (seeding pi's `trust.json`) so the agent never stalls at the dialog,
  gated like the claude/antigravity agent types -- silent under `mngr create --yes`
  or the new `auto_dismiss_dialogs` config, an interactive prompt otherwise, and
  it extends the grant automatically when the source repo is already trusted.

Known gaps carried for follow-up (matching the other ports): session preservation
on destroy, scheduled-deploy file/env contributions, a `waiting_reason` listing
column, the live streaming snapshot, and a per-agent permission-gate (pi runs tools
without a confirmation gate by default).
