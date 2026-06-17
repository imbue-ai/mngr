# Unabridged Changelog - mngr_opencode

Full, unedited changelog entries for the `mngr_opencode` project, consolidated nightly from individual files in `libs/mngr_opencode/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-15

Implemented the `waiting_reason` listing field for the `opencode` agent type, matching claude and codex. `mngr list` now reports *why* a WAITING opencode agent is blocked: `PERMISSIONS` when a tool is waiting on an approval prompt (an `ask` permission policy), or `END_OF_TURN` when the agent is idle with its turn complete.

The in-process lifecycle plugin now tracks opencode's `permission.asked` / `permission.replied` events, keeping a `permissions_waiting` marker present while any prompt is open (it tracks pending request ids, so concurrent prompts from task-tool subagents are handled). While that marker is present the agent is promoted from RUNNING to WAITING, so a blocking approval prompt no longer reads as RUNNING. A stranded prompt is cleared when the root turn ends, as a safety net. (The `@opencode-ai/sdk` type stubs name these events `permission.updated`/`permissionID` rather than the binary's `permission.asked`/`requestID`; the plugin accepts both since opencode self-upgrades.)

The `PERMISSIONS` reason is gated on the agent's `active` (in-turn) marker, so a stranded `permissions_waiting` marker that outlived its turn reports `END_OF_TURN` rather than wrongly showing `PERMISSIONS` -- the verdict no longer depends on the root-idle safety net having deleted the file. That gating rule is the shared `classify_waiting_reason` in mngr core, routed through both `get_lifecycle_state` and the `waiting_reason` field generator so the two readers cannot drift (the `WaitingReason` enum and the classifier are shared across the claude/codex/opencode plugins). As a second safety net (alongside the root-idle clear), the in-process plugin also clears any stranded marker at server startup -- a freshly started server has no pending prompts, so a marker left on disk by a prior killed/crashed server is stale.

Verified end-to-end against the live opencode binary (1.17.7) by a release test that creates a real `bash: ask` agent, triggers a tool call, and asserts the marker appears. Also verified live that, unlike codex (whose hook model strands the markers when a dialog is cancelled, briefly mislabeling the reason), opencode clears the marker promptly when a prompt is answered or aborted: a denial emits `permission.replied` and `session.idle`, and an abort emits `session.idle` -- so opencode does not have codex's cancelled-dialog limitation.

Clarified the README's note on the `waiting_reason` listing field. It is still unimplemented, but it is *doable in-process* (not blocked on an upstream change): an agent using an `ask` permission policy blocks on a prompt, and opencode's event bus emits `permission.asked` / `permission.replied`, which this plugin's extension already receives via its `event` hook -- so maintaining a marker on those events would surface a `PERMISSIONS` reason, the way the codex agent type does.

## 2026-06-12

# Real OpenCode agent support

The `opencode` agent type graduated from a bare config shell (which ran the
binary but reported WAITING forever, with no transcript, resume, or isolation)
to a full agent at roughly the `mngr_antigravity` level of parity. OpenCode is
architecturally unlike Claude Code / Antigravity -- a client-server app with
SQLite-backed sessions and no POSIX-sh hook mechanism -- so the implementation
runs each agent as a headless `opencode serve` plus an `opencode attach` TUI
client, maintains lifecycle/transcript via an in-process TypeScript plugin loaded
into the server, and sends messages over the server's HTTP API (see below).

User-visible changes:

- **RUNNING vs WAITING lifecycle.** A small in-process OpenCode plugin
  (auto-loaded from the per-agent config dir) maintains the `active` marker, so
  `mngr list` / idle detection correctly show the agent as RUNNING while it works
  and WAITING when it is done. It is subagent-aware: spawning task-tool subagents
  (child sessions) keeps the agent RUNNING until the whole turn finishes, because
  the marker clear is gated on the root session.
- **Conversation resume across stop/start.** `mngr stop` then `mngr start`
  resumes the prior conversation: the launch script records the root session id
  on first launch and re-attaches to it (`opencode attach --session <id>`) on
  restart, reading it back from the per-agent SQLite store, instead of starting
  fresh.
- **Transcripts.** `mngr transcript` now works for opencode agents. Both the raw
  transcript and (on session idle) the common-format transcript `mngr transcript`
  reads are written in-process by the plugin -- no background converter or
  supervisor. Gated by `emit_common_transcript` (default on).
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

Architecture: opencode agents run as a headless `opencode serve` plus an
`opencode attach` TUI client (rather than a single TUI driven by keystrokes),
exploiting that OpenCode is a client-server app. `mngr message` delivers messages
by POSTing to the agent's server (`prompt_async`); the attached client renders
them, so the conversation stays fully visible in `mngr connect` while sending is
robust and structured -- no tmux keystroke paste, and no race against OpenCode's
post-launch TUI repaint (which silently drops keystrokes and could lose the first
message under the earlier TUI-typing approach). The launch script pre-creates the
session (or reuses it on restart) so the client attaches to a known session and
resume works; the lifecycle plugin runs only in the server process (a role-gated
guard) so the marker/transcript have a single writer. This is covered by a
release test (`test_opencode_agent.py`) that drives the real `opencode` binary
through the full `mngr` CLI flow (create, RUNNING/WAITING, transcript, resume
across stop/start, recall) using OpenCode's free model; release tests do not run
in CI.

Not yet implemented (carried, like `mngr_antigravity`): session preservation on
destroy, scheduled-deploy file/env contributions, the `waiting_reason` listing
field, the live streaming snapshot, and clone-carries-conversation-forward.

Operational note: OpenCode self-upgrades, so the installed version is a moving
target (verified against 1.16.2); the integration is written to tolerate the
older/newer event shapes (`session.status` and the deprecated `session.idle`).
Version pinning / install management is a natural follow-up.

Added a node-harness conformance test asserting that opencode's real emitted
common-transcript records validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`) -- also the first CI-runnable check of
opencode's in-process TypeScript emitter (previously covered only by the non-CI release
test). The release test now runs on the shared agent release-lifecycle harness
(`imbue.mngr.agents.agent_release_testing`).

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
