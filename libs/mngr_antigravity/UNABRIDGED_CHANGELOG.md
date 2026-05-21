# Unabridged Changelog - mngr_antigravity

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-21: mngr-gemini-to-antigravity

Rename `mngr_gemini` to `mngr_antigravity`; agent type `gemini` is replaced by `antigravity`. Google announced on 2026-05-19 that the Gemini CLI is being superseded by the Antigravity CLI (`agy`); the legacy request path turns off for paid-tier accounts on 2026-06-18. The plugin was never released, so this is a destructive rename with no shim. The new CLI is architecturally closer to Claude Code than to Gemini CLI: process name is `agy`, hook event names match Claude's (`SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Stop`, `Notification`), and `--dangerously-skip-permissions` is the documented auto-allow flag. `auto_allow_permissions=True` is wired through that CLI flag rather than a permission hook. `pre_trust_workspace=True` (default) appends the agent's `work_dir` to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces` to suppress the first-launch "Do you trust this folder?" dialog. `emit_common_transcript=True` (default) wires the JSONL transcripts agy writes to `~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl` into mngr's common-transcript schema, scoped per-agent by grepping agy's own `--log-file` for `Created conversation <uuid>` lines.

## Pre-rename history (project was named mngr_gemini)

The entries below describe work done before the 2026-05-21 rename to `mngr_antigravity`. Module paths (`libs/mngr_gemini/...`) and class names (`GeminiAgent`, `GeminiAgentConfig`) refer to the project as it existed at the time of each entry. They were obsoleted by the rename and are retained here for historical context.

### 2026-05-14

Add `gemini` agent type plugin (`imbue-mngr-gemini`) that wires Google's Gemini CLI into mngr.

### mngr_gemini-feature-parity: seven-PR plan toward parity with mngr_claude

Add `specs/mngr-gemini-feature-parity/concise.md` mapping out a seven-PR plan to bring `mngr_gemini` closer to feature parity with `mngr_claude` (settings management, hook injection, lifecycle hookimpls, session adoption, headless variant, skill-provisioned subtypes). The two sibling-package PRs from an earlier draft (`mngr_gemini_usage`, `mngr_gemini_subagent_proxy`) are deferred.

Add `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py`, the foundation for the remaining PRs: read/write helpers for `~/.gemini/settings.json` and its workspace/system counterparts (atomic write with `.bak` backup, malformed-JSON-tolerant), env-var interpolation matching Gemini CLI's `$VAR` / `${VAR}` / `${VAR:-default}` syntax, two hook-config builders (`SessionStart` readiness sentinel and `BeforeTool` permission auto-allow), and merge helpers that skip duplicate matcher groups.

Wire the new readiness hook into `GeminiAgent`. `provision()` writes a small mngr-owned settings file to `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` containing the `SessionStart` hook, and `modify_env_vars()` points Gemini at it via `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (Gemini's documented system-tier override) plus `GEMINI_CLI_TRUST_WORKSPACE=true`. The previous `--skip-trust` default is dropped from `cli_args`. The user's workspace and `~/.gemini/` are not touched -- no `.gemini/` directory appears in the project, no merge with user files. After this change, `mngr` can detect a Gemini agent's readiness from `$MNGR_AGENT_STATE_DIR/session_started` instead of polling the rendered TUI.

Add an opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` (default `False`, mirroring `mngr_claude`). When enabled, `provision()` also installs a `BeforeTool` wildcard hook in the same system-settings file that auto-approves every tool call by emitting `{"decision":"allow"}` on stdout. Preferred over the `-y`/`--approval-mode yolo` CLI flag because the hook survives admin policies that disable yolo mode and shows up explicitly in Gemini's `--debug` hook-registry output.

Wire the readiness sentinel into `GeminiAgent.wait_for_ready_signal`. `mngr create gemini` and `mngr reconnect` now block on `$MNGR_AGENT_STATE_DIR/session_started` being touched by the `SessionStart` hook installed in PR2, instead of polling the rendered TUI banner exclusively. `assemble_command` prepends `rm -f` of the sentinel so a leftover from a previous run can't trick the ready-detection into succeeding before the new Gemini session has started. End-to-end smoke against Gemini CLI 0.42.0: the sentinel appeared ~2.4s after launch and ready-detection unblocked cleanly.

### mngr-gemini-transcript: common transcript readable by `mngr transcript`

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
