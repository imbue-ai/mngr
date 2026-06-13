# Unabridged Changelog - mngr_claude

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-12

`mngr create --adopt-session <session-id>` now resolves a bare session ID against more locations. In addition to the current and user-scope Claude config dirs (`$CLAUDE_CONFIG_DIR/projects/` and `~/.claude/projects/`), it now also searches every live local mngr agent's per-agent config dir and the preserved session files of destroyed agents (see `preserve_sessions_on_destroy`). Passing a full `.jsonl` path is unchanged. Only the local host dir is scanned for mngr agent and preserved sessions.

Clarified the `--adopt-session` help text and behavior: the option is repeatable, but when multiple sessions are named, every named session is made available in the new agent while only the last one is resumed on startup (Claude can only resume one session at a time).

Internal: routed the plugin's `host_dir / "agents"` path constructions through the shared `get_agents_root_dir` / `get_agent_state_dir_path` helpers (now defined in `imbue.mngr.hosts.common`). No behavior change.

The `.claude/settings.local.json` gitignore preflight/provisioning check now delegates to the shared `check_path_gitignore_status` helper in `mngr.api.git` rather than implementing the git-check-ignore logic inline. No user-visible behavior change.

Added a conformance test asserting that claude's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`), so the claude emitter and the shared
contract cannot drift apart.

## 2026-06-11

### Fixed

- Fixed: Provisioning a local Claude agent no longer creates self-referential symlink loops inside the user's shared `~/.claude/` (e.g. `~/.claude/skills/skills -> ~/.claude/skills`, `~/.claude/commands/commands`, `~/.claude/plugins/cache/cache`). `_sync_user_resources` used plain `ln -sf` as an idempotent command; on the second and later provisions the destination was already a symlink-to-directory, so `ln` dereferenced it and nested a new link inside the shared source. All sync symlinks (directory, child, and individual-file, including credentials and `keybindings.json`) now use `ln -sfn` (`--no-dereference`), which replaces the existing destination symlink instead of following it.

### Changed

- Changed: A skill-provisioned agent's primary skill (e.g. `code-guardian`, `fixme-fairy`) is now installed into that agent's own per-agent config dir (`$CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md`) instead of the user's global `~/.claude/skills/`. Previously the local install wrote to global `~/.claude/skills/`, which `_sync_user_resources` then symlinked into every local agent's config dir, so a `code-guardian`/`fixme-fairy` skill leaked into the skill list of every local agent. To support this, `skills/` is now synced via child-level symlinks (one symlink per skill, mirroring how `plugins/` is already handled) so the agent's own skill can live as a real file alongside the symlinked user skills without leaking back into the shared source. The local install is now silent (no interactive install/update prompt), matching the always-silent remote install.

## 2026-06-10

Test-quality hardening across the mngr_claude test suite (no user-visible behavior change). Replaced assertions that passed without verifying correctness with ones that check real effects:

- Seven `on_before_provisioning` tests that asserted nothing ("did not raise") now assert observable effects (config left untouched, missing-credentials warning present/absent, untrusted worktree rejected).
- `does-not-extend-trust` provisioning tests now assert the exact set of trusted projects instead of the presence of a key the test itself wrote.
- Transcript-converter truncation tests now assert exact lengths and the ellipsis marker (not just an upper bound), and the "skips event" tests now prove only the bad event is dropped (a known-good event survives) plus cover the missing-`timestamp` branch.
- Command-assembly and install-command tests now assert on shlex-parsed tokens / load-bearing flags instead of hand-rebuilt exact shell strings.
- Skill-install skip tests assert file content is unchanged instead of relying on mtime equality; added remote-install coverage. Custom-agent-type resolution test now sets a non-default field to prove it survives the merge.
- Grace-period headless test asserts the poll actually re-checked; removed dead `_patch_agent_as_stopped` calls and a fragile wall-clock timing assertion.
- claude_config no-op tests assert content is byte-for-byte unchanged; effort-callout check test isolated to the effort dialog.
- Removed an introduced `unittest.mock.patch` of the function under test in favor of the real no-credentials environment, and a duplicate local `temp_source_dir` fixture (now inherited from the shared modal conftest).
- Release/acceptance tests: the Modal provisioning test destroys the agent and asserts a non-empty preserved session JSONL; the adopt-session and modal tests drop brittle "Done." log-string checks; the no-dialog send_message test matches the specific downstream timeout; the background-tasks prefix-collision test asserts the script reached its gone-session exit; magic `sleep` literals replaced with a named constant.

- `ClaudeAgent` now supplies its own "message accepted" probe to the shared submission-confirm path: a shell command that reads the latest `enqueue` event from Claude's transcript event log (`logs/claude_transcript/events.jsonl`) and prints its ISO-8601 timestamp. This is the Claude-specific knowledge that previously lived (hardcoded) in the shared `tui_utils` module; moving it into the plugin keeps `tui_utils` agent-neutral while preserving the existing fast-confirm-on-enqueue behavior for Claude agents.

Raised the stale coverage floor from 24% to 80% to match the coverage CI already measures (~83%), and removed the now-obsolete comment referencing per-package offload coverage drift (the offload bug that caused that drift has since been fixed).

## 2026-06-09

- Readiness hooks: a Claude agent restarted/resumed mid-turn no longer reports the RUNNING lifecycle state forever. The `active` marker is set on UserPromptSubmit and only removed by the Stop / idle Notification hooks, so a turn abandoned by an abnormal exit (container restart, OOM, crash) left it stale and the agent stuck at RUNNING. The SessionStart hook now clears `active`/`permissions_waiting` on `startup`/`resume` (a fresh, not-mid-turn process), so the lifecycle state self-heals on the next (re)start; `compact` is excluded because auto-compaction fires mid-turn while Claude is genuinely active. The same hook touches a new `claude_process_started` marker whose mtime gives consumers a restart boundary to compare transcript timestamps against (any transcript event older than it belongs to a turn the current process did not run). The shared "clear active markers and emit an activity event" shell snippet is extracted into a constant reused by the Notification idle hook and the new SessionStart hook so the two stay byte-identical.

Claude session preservation on destroy was rewritten onto the new shared
`preserve_agent_data` machinery in core mngr. Behavior is unchanged in substance -- session
JSONLs, the raw and common transcripts, and the session-id history are still preserved before
the agent state directory is deleted, and `projects/` is still skipped in `use_env_config_dir`
mode -- but the implementation is now a single declarative list of files preserved through one
code path for both online and offline (volume-backed) hosts, replacing the previously
duplicated SSH and Volume implementations.

The on-disk layout of preserved sessions changed: files now live at
`<local_host_dir>/preserved/<agent-name>--<agent-id>/` and mirror the agent state directory
verbatim (e.g. `plugin/claude/anthropic/projects/...`, `logs/claude_transcript/...`,
`events/claude/common_transcript/...`, `claude_session_id_history`), instead of the old
`<local_host_dir>/plugin/mngr_claude/preserved_sessions/<agent-name>--<agent-id>/` location
with renamed subdirectories. This is a switch-forward change; previously preserved sessions in
the old location are left in place.

## 2026-06-08

Internal refactor (no behavior change): the `claude` agent's `assemble_command` now shell-quotes its extra `agent_args` via the shared `quote_agent_args` helper instead of an inline `shlex.quote` loop. This is the same quoting the plugin already performed; it now shares one implementation (and one explanatory comment) with `BaseAgent`, so the two cannot drift.

Standardized mngr_claude's test setup on `register_plugin_test_fixtures(globals())`
for HOME isolation (matching every other mngr plugin), keeping
`pytest_plugins = ["imbue.mngr_modal.conftest"]` only to share mngr_modal's test
fixtures (`modal_subprocess_env`, etc.). Removed the now-redundant
`enabled_plugins` override. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-06

Added approximate response streaming for Claude agents, driven by watching the agent's tmux pane.

- New `streaming_snapshot_interval_seconds` (float, default `0.0`) on the `claude` agent type config. When `> 0`, a background watcher polls the agent's tmux pane every N seconds and writes the in-progress assistant text to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`. When `<= 0` (the default) the watcher is neither provisioned nor run, so existing behavior is unchanged.
- `stream_buffer` format: line 1 is the `uuid` of the last *complete* assistant message (empty string if none yet, read from `logs/claude_transcript/events.jsonl`), and lines 2+ are the in-progress assistant text reverse-mapped from the terminal rendering back into markdown. It is written atomically (temp file + `mv`), cleared on watcher startup, and emptied (body cleared, id line kept) when the agent goes idle.
- New self-contained watcher script `resources/stream_snapshot.py` (stdlib-only, like `sync_keychain_credentials.py`). It captures the pane with `tmux capture-pane -e -J` (ANSI codes preserved, soft-wraps rejoined), identifies the latest assistant-text block by the `●` marker's color (assistant markers are the achromatic default text color; chromatic tool-call and mid-gray status markers are ignored), and reverse-maps bold/italic, inline code, links (OSC 8 hyperlinks), blockquotes, lists, code blocks, and tables (box-drawing back to pipe syntax). The pure parsing functions are unit-tested directly against real captured fixtures.
- The body is strict-append within a message: successive visible-pane snapshots are overlap-stitched (longest suffix/prefix line match) so the full message is reconstructed as it scrolls; a stale shorter snapshot that is a prefix of the accumulated body is ignored, and a genuinely non-overlapping snapshot resets to a new message. A trailing table is held back until its raw form is stable across polls, then rendered, and each successive render is a superset of the previous one so the rendered body never shrinks or duplicates.
- The poll interval is provisioned to a per-agent file (`plugin/claude/stream_interval`) that the watcher reads at runtime, rather than relying on env-var propagation into the background-tasks subshell. `claude_background_tasks.sh` launches and restarts the watcher whenever the script is present (the provision-time presence check is the single gate, like the common-transcript converter). Provisioning fails fast if streaming is enabled but the host lacks `python3`.
- Added `ClaudeAgent.get_stream_buffer_path()` so other code (e.g. `mngr robinhood`) can locate and read the buffer.

Not in scope: perfect markdown fidelity, heading-level or code-block-language recovery, reasoning/"thinking" block streaming, streaming for non-claude agent types, and any new top-level `mngr` CLI command to read the buffer.

## 2026-06-05

Internal refactor (no behavior change): `ClaudeAgent._find_git_source_path` now delegates to the shared core helper `imbue.mngr.utils.git_utils.find_git_source_path` instead of inlining the `find_git_common_dir` + parent logic, which was duplicated in the `antigravity` plugin.

Updated changelog references following the `mngr_uncapped_claude` plugin rename:
mentions of the `mngr uncapped-claude` command in this project's changelog now
read `mngr robinhood`. No code changes.

## 2026-06-04

Replaced the module-local `_get_local_host` helper with the shared `get_local_host` from `imbue.mngr.api.providers` (deduplication; no behavior change).

## 2026-06-02

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

Fixed `--adopt-session` rejecting valid Claude agent subtypes. It now accepts any agent type that resolves to a Claude agent (including config-defined templates like `write-plus` whose `parent_type` chain reaches `claude`), instead of only the literal `claude` type name. The check routes through the centralized `resolve_agent_type` registry rather than a string comparison.

# Simplify `--adopt-session` agent-type validation

- Now that `CreateAgentOptions.agent_type` is always set (it became a
  required field), the `--adopt-session` `on_before_create` validation no
  longer special-cases an unset type: it simply requires the agent type
  to be `claude`. No behavior change for users, since the CLI already
  requires a concrete agent type.

Updated the `on_before_create` hook implementation (used for `--adopt-session` validation) to accept the new `mngr_ctx` parameter now passed by mngr.

## 2026-05-28

# Adopt-session test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The `mngr_claude`
adopt-session tests hand-roll a trusted-subprocess profile and load it, so the
`trusted_subprocess_env` fixture now writes `is_allowed_in_pytest = true` into
that profile's settings.local.toml. Test-only change; no user-facing behavior
change.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

- `ClaudeAgentConfig.merge_with` follows mngr's new assign-by-default semantics: an override's `cli_args` replaces the base's (rather than concatenating). To opt back into additive layering, use the `__extend` operator with an explicit list value, e.g. `cli_args__extend = ["--verbose"]`; the string-shorthand form that the bare `cli_args` field accepts (which the validator splits via shlex) is not accepted by the `__extend` resolver. See the `mngr` changelog entry for the full breaking-change writeup.

Update Claude plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` and `_preflight_send_message` now
take `tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `claude_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit
when a Claude agent's session was killed but a sibling session whose name
shares this name as a prefix was still alive, leaking the transcript
streamer and common-transcript converter for stopped agents.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

`resolve_shared_claude_config_dir()` (used when a claude agent opts into `use_env_config_dir=True`) now falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset, instead of raising. The fallback matches claude's own default, so callers can treat that flag as a pure "don't touch the config dir" knob even on machines where the user never sets `CLAUDE_CONFIG_DIR`. Also drops `ORIGINAL_CLAUDE_CONFIG_DIR` from the agent env in the `mngr robinhood` flow so credential sync reads from the live `$CLAUDE_CONFIG_DIR` (matters when robinhood is invoked from inside another mngr claude agent).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`ClaudeAgent` now satisfies the new `HasTranscriptMixin` and
`HasCommonTranscriptMixin` mixins on `AgentInterface` (introduced to give every
agent type a shared transcript-capture contract). The user-visible behavior of
`mngr transcript <claude-agent>` is unchanged.

## 2026-05-14

- Fixed: a cloned claude agent now actually resumes the source agent's conversation (the model sees and acts on the source's history), not just inherits the session JSONL on disk. Previously, after #1598's cross-host plugin/ rsync, claude on the destination would still start fresh because the JSONL was filed under the *source's* encoded work_dir, the rsynced ``sessions-index.json`` pointed at source paths, and ``claude_session_id`` was wrong. ``_adopt_cloned_session`` now renames the project subdir to the destination's realpath-resolved encoding (handles the ``/mngr/projects/agent-X`` → ``/__modal/volumes/<vol-id>/projects/agent-X`` symlink on Modal), drops the stale index, writes ``claude_session_id`` to the JSONL filename's stem (the ground truth — the source's own ``claude_session_id`` file holds the agent UUID from the SessionStart hook default rather than the real id), and carries forward ``claude_session_id_history``. The ``--adopt-session`` flow shares the same finalize step.

Add a `use_env_config_dir` option on the `claude` agent type config. When set
to `true`, local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead of
provisioning a per-agent config dir, and mngr does not write to the user's
Claude config (no trust additions, dialog dismissal, per-agent settings, or
keychain provisioning). Only supported for local hosts; `$CLAUDE_CONFIG_DIR`
must be set. The user is responsible for one-time interactive `claude` setup.
See `libs/mngr_claude/README.md` for details.

## 2026-05-06

- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
