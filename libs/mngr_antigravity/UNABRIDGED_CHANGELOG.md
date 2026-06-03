# Unabridged Changelog - mngr_antigravity

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_antigravity/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-01

The `antigravity` agent type now uses agy hooks to report lifecycle state (verified working against agy 1.0.3).

- mngr provisions a per-agent `hooks.json` and points agy at it with `--add-dir` (via a `/tmp` symlink, since agy rejects the dotted state-dir path), so the user's global `~/.gemini/config/` is untouched and each agent's state stays isolated.
- A `PreInvocation`/`Stop` hook pair maintains an `active` marker so antigravity agents now report RUNNING while working and WAITING when idle (previously they had no `active` marker and could not report RUNNING).
- `auto_allow_permissions = true` continues to use the `--dangerously-skip-permissions` CLI flag. agy's documented `PreToolUse` `{"decision": "allow"}` hook output does not actually gate the `run_command` confirmation dialog, so a hook can't replace the flag.

Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the hook execution engine never runs (it executes hooks only from `~/.gemini/config/hooks.json` and workspace `.agents/`; the TUI path is loaded for display only -- agy bug, reported as antigravity-cli#49). mngr writes its own per-agent file via `--add-dir` and does not rely on the TUI.

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

Rename `mngr_gemini` to `mngr_antigravity`; agent type `gemini` is replaced by `antigravity`. Google announced on 2026-05-19 that the Gemini CLI is being superseded by the Antigravity CLI (`agy`); the legacy request path turns off for paid-tier accounts on 2026-06-18. The plugin was never released, so this is a destructive rename with no shim. The new CLI is architecturally closer to Claude Code than to Gemini CLI: process name is `agy`, hook event names match Claude's (`SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Stop`, `Notification`), and `--dangerously-skip-permissions` is the documented auto-allow flag. `auto_allow_permissions=True` is wired through that CLI flag rather than a permission hook. The first-launch "Do you trust this folder?" dialog is dismissed Claude-style (mirroring `mngr_claude`'s `interactively_dismiss_claude_dialogs`): under `mngr create --yes` or `auto_dismiss_dialogs=True` (per-agent-type opt-in, default `False`) the agent's `work_dir` is silently appended to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces`; in interactive shells mngr prompts via `click.confirm` before mutating the file; non-interactive shells without either opt-in raise `UserInputError`. There is no `GEMINI_CLI_TRUST_WORKSPACE` env-var analog in agy 1.0.0, so the user-tier settings file is the only place to register trust. `emit_common_transcript=True` (default) wires the JSONL transcripts agy writes to `~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl` into mngr's common-transcript schema, scoped per-agent by grepping agy's own `--log-file` for `Created conversation <uuid>` lines. The readiness sentinel that `mngr_gemini` shipped is **not** re-introduced -- live testing showed agy loads `hooks.json` correctly but hook execution is gated behind the `json-hooks-enabled` experiment flag (Google-controlled); once the flag is GA the sentinel can come back.

- `AntigravityAgentConfig.merge_with` follows mngr's new assign-by-default semantics: an override's `cli_args` replaces the base's (rather than concatenating). To opt back into additive layering, use the `__extend` operator with an explicit list value, e.g. `cli_args__extend = ["--verbose"]`; the string-shorthand form that the bare `cli_args` field accepts (which the validator splits via shlex) is not accepted by the `__extend` resolver. See the `mngr` changelog entry for the full breaking-change writeup.

Update the Antigravity plugin to use the structured `TmuxWindowTarget` type for
tmux pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `antigravity_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit when an
Antigravity agent's session was killed but a sibling session whose name shares
this name as a prefix was still alive, leaking the transcript streamer and
common-transcript converter for stopped agents.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Add `specs/mngr-gemini-feature-parity/concise.md` mapping out a seven-PR plan to bring `mngr_gemini` closer to feature parity with `mngr_claude` (settings management, hook injection, lifecycle hookimpls, session adoption, headless variant, skill-provisioned subtypes). The two sibling-package PRs from an earlier draft (`mngr_gemini_usage`, `mngr_gemini_subagent_proxy`) are deferred.

Add `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py`, the foundation for the remaining PRs: read/write helpers for `~/.gemini/settings.json` and its workspace/system counterparts (atomic write with `.bak` backup, malformed-JSON-tolerant), env-var interpolation matching Gemini CLI's `$VAR` / `${VAR}` / `${VAR:-default}` syntax, two hook-config builders (`SessionStart` readiness sentinel and `BeforeTool` permission auto-allow), and merge helpers that skip duplicate matcher groups.

Wire the new readiness hook into `GeminiAgent`. `provision()` writes a small mngr-owned settings file to `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` containing the `SessionStart` hook, and `modify_env_vars()` points Gemini at it via `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (Gemini's documented system-tier override) plus `GEMINI_CLI_TRUST_WORKSPACE=true`. The previous `--skip-trust` default is dropped from `cli_args`. The user's workspace and `~/.gemini/` are not touched -- no `.gemini/` directory appears in the project, no merge with user files. After this change, `mngr` can detect a Gemini agent's readiness from `$MNGR_AGENT_STATE_DIR/session_started` instead of polling the rendered TUI.

Add an opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` (default `False`, mirroring `mngr_claude`). When enabled, `provision()` also installs a `BeforeTool` wildcard hook in the same system-settings file that auto-approves every tool call by emitting `{"decision":"allow"}` on stdout. Preferred over the `-y`/`--approval-mode yolo` CLI flag because the hook survives admin policies that disable yolo mode and shows up explicitly in Gemini's `--debug` hook-registry output.

Wire the readiness sentinel into `GeminiAgent.wait_for_ready_signal`. `mngr create gemini` and `mngr reconnect` now block on `$MNGR_AGENT_STATE_DIR/session_started` being touched by the `SessionStart` hook installed in PR2, instead of polling the rendered TUI banner exclusively. `assemble_command` prepends `rm -f` of the sentinel so a leftover from a previous run can't trick the ready-detection into succeeding before the new Gemini session has started. End-to-end smoke against Gemini CLI 0.42.0: the sentinel appeared ~2.4s after launch and ready-detection unblocked cleanly.

# Gemini agents now produce a common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

The gemini plugin also captures the *raw* gemini session JSONL verbatim into
`logs/gemini_transcript/events.jsonl`. This preserves every field gemini emits (model
metadata, internal blocks, etc.) and lives inside the agent state dir, so the transcript
survives cleanup of gemini's own `~/.gemini/tmp/` working directories.

`GeminiAgent` satisfies the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts` and
shipping the matching per-agent scripts.

## 2026-05-14

Add `gemini` agent type plugin (`imbue-mngr-gemini`) that wires Google's Gemini CLI into mngr.

## mngr_gemini-feature-parity: seven-PR plan toward parity with mngr_claude

Add `specs/mngr-gemini-feature-parity/concise.md` mapping out a seven-PR plan to bring `mngr_gemini` closer to feature parity with `mngr_claude` (settings management, hook injection, lifecycle hookimpls, session adoption, headless variant, skill-provisioned subtypes). The two sibling-package PRs from an earlier draft (`mngr_gemini_usage`, `mngr_gemini_subagent_proxy`) are deferred.

Add `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py`, the foundation for the remaining PRs: read/write helpers for `~/.gemini/settings.json` and its workspace/system counterparts (atomic write with `.bak` backup, malformed-JSON-tolerant), env-var interpolation matching Gemini CLI's `$VAR` / `${VAR}` / `${VAR:-default}` syntax, two hook-config builders (`SessionStart` readiness sentinel and `BeforeTool` permission auto-allow), and merge helpers that skip duplicate matcher groups.

Wire the new readiness hook into `GeminiAgent`. `provision()` writes a small mngr-owned settings file to `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` containing the `SessionStart` hook, and `modify_env_vars()` points Gemini at it via `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (Gemini's documented system-tier override) plus `GEMINI_CLI_TRUST_WORKSPACE=true`. The previous `--skip-trust` default is dropped from `cli_args`. The user's workspace and `~/.gemini/` are not touched -- no `.gemini/` directory appears in the project, no merge with user files. After this change, `mngr` can detect a Gemini agent's readiness from `$MNGR_AGENT_STATE_DIR/session_started` instead of polling the rendered TUI.

Add an opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` (default `False`, mirroring `mngr_claude`). When enabled, `provision()` also installs a `BeforeTool` wildcard hook in the same system-settings file that auto-approves every tool call by emitting `{"decision":"allow"}` on stdout. Preferred over the `-y`/`--approval-mode yolo` CLI flag because the hook survives admin policies that disable yolo mode and shows up explicitly in Gemini's `--debug` hook-registry output.

Wire the readiness sentinel into `GeminiAgent.wait_for_ready_signal`. `mngr create gemini` and `mngr reconnect` now block on `$MNGR_AGENT_STATE_DIR/session_started` being touched by the `SessionStart` hook installed in PR2, instead of polling the rendered TUI banner exclusively. `assemble_command` prepends `rm -f` of the sentinel so a leftover from a previous run can't trick the ready-detection into succeeding before the new Gemini session has started. End-to-end smoke against Gemini CLI 0.42.0: the sentinel appeared ~2.4s after launch and ready-detection unblocked cleanly.

## mngr-gemini-transcript: common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

The gemini plugin also captures the *raw* gemini session JSONL verbatim into
`logs/gemini_transcript/events.jsonl`. This preserves every field gemini emits (model
metadata, internal blocks, etc.) and lives inside the agent state dir, so the transcript
survives cleanup of gemini's own `~/.gemini/tmp/` working directories.

`GeminiAgent` satisfies the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts` and
shipping the matching per-agent scripts.
