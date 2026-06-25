# Unabridged Changelog - mngr_opencode

Full, unedited changelog entries for the `mngr_opencode` project, consolidated nightly from individual files in `libs/mngr_opencode/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

No production behavior change. The OpenCode agent-config merge test now exercises the unified overlay merge pipeline (`merge_models_via_overlay`) instead of the removed `AgentTypeConfig.merge_with` helper, so it locks in the same behavior: an override's `cli_args` and `config_overrides` replace the base. The merge-rejects-other-type test was dropped because that type guard now lives in the shared `merge_models_via_overlay` path (covered by mngr's own tests).

Trimmed the README to user-relevant content (removed internal implementation details and roadmap notes) and tightened it for concision.

Aligned the `OpenCodeAgentConfig` field descriptions (surfaced via `mngr config`/help) with the concise wording in the README options table.

The config table now documents `command`, `check_installation`, `version`, `update_policy`, and `preserve_on_destroy` (`version` / `update_policy` had been dropped from the table).

## 2026-06-18

Added a `version` field to the opencode agent type that pins the installed opencode CLI: installation runs the opencode installer with `VERSION=<version>` and provisioning verifies the installed opencode matches, erroring on a mismatch.

Added an `update_policy` field that governs opencode's startup auto-update. `NEVER` writes `"autoupdate": false` into the per-agent `opencode.json` so opencode does not update itself on launch; `AUTO` leaves auto-update enabled; `ASK` behaves like `AUTO`. When unset, it defaults to `NEVER` (auto-update disabled) -- set `AUTO` to re-enable it. An explicit `autoupdate` key in `config_overrides` always wins.

Opencode agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting. (The behavior comes from the shared base agent; the opencode lifecycle test was updated to name its primary window accordingly.)

## 2026-06-17

The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (per-resource permission policy via the `permission` config-override key).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `opencode` CLI is installed and installs it (`curl -fsSL https://opencode.ai/install | bash`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. A new `check_installation` config field (default `True`) disables the check when set to `False`.

The auto-allow permission apply-path (the wildcard `permission` config) now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`OpenCodeAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.

`OpenCodeAgent` now also declares `InteractiveAgentMixin` -- the marker for agents that accept interactive messages, now that `send_message` is no longer a universal `AgentInterface` method. OpenCode already implemented `send_message` (it POSTs to its server), so this only adds the marker. Behavior is unchanged.

Added OpenCode session adoption: a newly created opencode agent can now resume an existing OpenCode conversation (via `--adopt` and/or `--from`) instead of starting fresh, including multi-session merge into the single `opencode.db` and rebinding each session's stored worktree path to the new agent's work dir.

At create time the plugin resolves an adopt argument -- a `ses_...` session id (searched across the user-native opencode db and every live/preserved mngr agent's db) or an absolute path to a source `opencode.db` -- then copies the resolved SQLite db (and its `-wal`/`-shm` sidecars) into the new agent's data dir, checkpoints the WAL, rebinds the session's stored source-worktree path to the new agent's work dir (`session.directory`, `project.worktree`, and the `project_directory` upsert), and writes the adopted session id into the resume pointer so the agent's first launch attaches to it.

Triggered by the central `--adopt` flag (`mngr create opencode --adopt <id-or-db-path>`; `--adopt-session` is accepted as an alias). The flag may be passed more than once: because OpenCode's store is a single `opencode.db`, the first adopted session is copied in as a fresh db and each subsequent one is merged into it (its `session` row plus descendant sub-sessions, owning `project`/`permission`/`project_directory` rows, and every `message`/`part`/`todo`/`session_share` row), so all named sessions end up available in the new agent's session switcher. OpenCode resumes one root conversation, so the last named session is the one resumed. Parity with the claude adopt resolver: adoption works from a preserved (destroyed) agent, a live mngr agent, and a plain-CLI run.

`--adopt` and `--from` can be combined: every named session is merged in alongside the `--from` clone, and the clone is the session resumed. A `--from <agent>` clone of an opencode agent resumes the source agent's conversation: a generic clone copies the source workspace but not its state dir, so the plugin transfers just the source's native opencode store (`opencode.db` + its `-wal`/`-shm` sidecars), reads the source's root session id from that store, rebinds it to the clone's work dir, and writes the resume pointer. Because a `--from` clone is fundamentally a workspace clone (carrying the conversation forward is a bonus), a source with no opencode store warns and starts fresh rather than failing; an explicit `--adopt` with an unknown session still errors.

A bad or ambiguous `--adopt` id is now reported as a clean error before any host or worktree is created, rather than surfacing as a traceback during provisioning.

The adopt value is now read from the first-class `CreateAgentOptions.adopt_session` field (and `OnBeforeCreateArgs.agent_options.adopt_session`) instead of the previous `plugin_data["adopt_session"]` namespaced key, following the core migration that promoted it to a typed option.

Adoption no longer requires the `sqlite3` command-line tool on the destination host. The agent's `opencode.db` is now assembled entirely on a local staging copy -- the first source db is copied in, each subsequent `--adopt` (and the `--from` clone) is merged in, and every adopted session is rebound to the new agent's work dir, all via Python's bundled SQLite -- and the finished db is copied onto the (possibly remote) host once at the end. A `--from` clone whose source lives on a remote host has that source db pulled across first. This removes a fragile dependency that could fail on hosts lacking the `sqlite3` CLI.

A corrupt or unreadable opencode db encountered while searching for an adopt session id is now logged as a warning (it is a real anomaly) rather than at trace level; resolution still continues against the other stores.

The opencode common-transcript emitter now emits `finish_reason` instead of `stop_reason` on assistant records (aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array built by walking the message's parts in arrival order, so the text/tool-call interleaving is preserved (`parts_ordered` true).

## 2026-06-16

OpenCode agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before an opencode agent's state directory is deleted on destroy, its raw and common transcripts and the root session-id history are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The opencode release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `opencode` binary.

- OpenCode's native resumable session store (the SQLite `opencode.db` plus its `-wal`/`-shm` write-ahead-log sidecars, and `storage/`) is now also preserved on destroy, targeting those specific paths so the session can be resumed/adopted; the sibling `auth.json` (a symlink to shared credentials) and `log/` are deliberately excluded. The WAL sidecars are copied alongside the db so recent (not-yet-checkpointed) turns are not lost.

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
