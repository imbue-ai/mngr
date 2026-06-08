# Unabridged Changelog - mngr_claude

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
