# Real OpenCode agent support

The `opencode` agent type graduated from a bare `BaseAgent` shell (which ran the
binary but reported WAITING forever, with no transcript, resume, or isolation)
to a first-class `InteractiveTuiAgent`, bringing it to roughly the
`mngr_antigravity` level of parity. OpenCode is architecturally unlike Claude
Code / Antigravity -- a client-server app with SQLite-backed sessions and no
POSIX-sh hook mechanism -- so the implementation leans on OpenCode's own
in-process TypeScript plugin extension point and its config-dir env vars.

User-visible changes:

- **RUNNING vs WAITING lifecycle.** A small in-process OpenCode plugin
  (auto-loaded from the per-agent config dir) maintains the `active` marker, so
  `mngr list` / idle detection correctly show the agent as RUNNING while it works
  and WAITING when it is done. It is subagent-aware: spawning task-tool subagents
  (child sessions) keeps the agent RUNNING until the whole turn finishes, because
  the marker clear is gated on the root session.
- **Conversation resume across stop/start.** `mngr stop` then `mngr start`
  resumes the prior conversation (via `opencode --continue`, which resumes the
  most recent root session from the per-agent SQLite store) instead of starting
  fresh.
- **Transcripts.** `mngr transcript` now works for opencode agents. The raw
  transcript is captured in-process by the plugin; a background converter turns
  it into the common format `mngr transcript` reads. Gated by
  `emit_common_transcript` (default on).
- **Per-agent isolation.** Each agent gets its own OpenCode config dir
  (`OPENCODE_CONFIG_DIR`) and data dir (`XDG_DATA_HOME`), so model, permission
  policy, sessions, and credentials are per-agent and never touch the user's
  global OpenCode state.
- **Shared auth.** By default the per-agent `auth.json` symlinks to the user's
  shared `~/.local/share/opencode/auth.json`, so a single `opencode auth login`
  in any agent authenticates them all (set `symlink_auth = false` for full
  isolation).

New `opencode` agent-type config options:

- `config_overrides` -- key/value blob merged last into the per-agent
  `opencode.json` (e.g. `model`, the `permission` policy block).
- `sync_global_config` (default true) -- base the per-agent config on a copy of
  the user's `~/.config/opencode/opencode.json`.
- `symlink_auth` (default true) -- symlink vs copy the shared `auth.json`.
- `auto_allow_permissions` (default false) -- inject a wildcard allow into the
  per-agent permission policy (auto-approve everything not explicitly denied).
- `emit_common_transcript` (default true) -- emit the common transcript.

Reliability: OpenCode briefly ignores keystrokes right after its TUI first
paints (its embedded server finishes initializing and the client repaints), so
the first `mngr message` to a fresh agent could be silently dropped. The send
now self-heals -- it confirms the paste echoed and, on a drop, clears the input
and re-sends -- so messages land reliably (a stable agent still lands on the
first attempt with no added latency). This is covered by a release test
(`test_opencode_agent.py`) that drives the real `opencode` binary through the
full `mngr` CLI flow (create, RUNNING/WAITING, transcript, resume across
stop/start) using OpenCode's free model; release tests do not run in CI.

Not yet implemented (carried, like `mngr_antigravity`): session preservation on
destroy, scheduled-deploy file/env contributions, the `waiting_reason` listing
field, the live streaming snapshot, and clone-carries-conversation-forward.

Operational note: OpenCode self-upgrades, so the installed version is a moving
target (verified against 1.16.2); the integration is written to tolerate the
older/newer event shapes (`session.status` and the deprecated `session.idle`).
Version pinning / install management is a natural follow-up.
