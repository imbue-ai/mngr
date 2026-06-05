# Unabridged Changelog - mngr_antigravity

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_antigravity/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-05

`antigravity` agents now stay RUNNING while a subagent or backgrounded task they launched is still working, instead of flipping to WAITING the moment the root agent's turn ends.

- The `Stop` hook no longer clears the `active` lifecycle marker on any `fullyIdle:true`. agy runs the Stop hooks for *every* conversation -- the root agent and each subagent it launches share the same hook -- and a subagent fires its own `"fullyIdle":true` Stop when it finishes, which can arrive while the root agent is still working. Clearing on that would wrongly report WAITING mid-turn.
- `PreInvocation` now runs `set_active_marker.sh`, which touches the marker and records the turn's *root* conversation (the one that opened the turn, seen while the marker was absent) in `root_conversation`. `Stop` runs `clear_active_marker_when_idle.sh`, which clears the marker only when the payload's conversation id matches that root **and** reports `"fullyIdle":true`. The root is re-recorded at each turn boundary, so `/clear`, `/fork`, `/switch`, and resume stay correct.
- The marker drives `BaseAgent`'s RUNNING/WAITING detection (present => RUNNING, absent => WAITING).
- Conversation resume is centralized on the same root-conversation tracking: on `mngr start` an antigravity agent now resumes its *main* conversation from `root_conversation` rather than the last line of the conversation-ids file. Previously, because subagents also append to that file, a stop/start could resume a subagent's conversation instead of the agent's own. The conversation-ids file is now used only as the set of conversations to scope transcript streaming, and `capture_conversation_id.sh` records each distinct id once (order/recency no longer matter).
- Verified live against agy 1.0.5: a backgrounded shell task produced the interim `fullyIdle:false` then final `fullyIdle:true` root Stop; a user message sent mid-flight (while the background task ran) kept the marker held and the root conversation unchanged; and a subagent's own `fullyIdle:true` Stop (a different conversation id) did not clear the root's marker, which was cleared only by the root's final Stop.

Each `antigravity` agent now runs `agy` under its own per-agent `$HOME` (at `<agent_state_dir>/plugin/antigravity/home/`), giving each agent its own permission policy, model, and isolated config/transcript/session state instead of today's all-or-nothing `--dangerously-skip-permissions` and shared global `~/.gemini`. Two new agent-type config fields:

- `settings_overrides` (dict, default `{}`) -- a free-form blob merged last into the per-agent `settings.json`, covering `permissions` (`{allow, deny, ask}`, precedence Deny > Ask > Allow), `toolPermission`, and `model` (an `agy models` display name). Mirrors `mngr_claude`'s field of the same name.
- `sync_home_settings` (bool, default `true`) -- base the per-agent `settings.json` on a copy of the user's real (global) `settings.json`, with `settings_overrides` layered on top; `false` starts from an empty base. This copies only agy's *global* `settings.json` scope (in practice theme/telemetry/trust); the user's model, permission grants, and behavioral policies live in other agy config scopes (`config/config.json`, per-project `config/projects/<uuid>.json`) that are intentionally not read, so set per-agent model/permissions via `settings_overrides`.
- `symlink_oauth_token` (bool, default `true`) -- symlink each agent's `antigravity-oauth-token` to the shared `~/.gemini` token (enables write-through sharing/propagation, see below) vs copy it for full per-agent isolation.

Other changes:

- Trust now splits by what is persisted: the durable source-repo path goes to the user's global settings (so trust isn't re-prompted across agents/worktrees of the same repo), while the transient per-agent workspace path goes only into the per-agent settings. Consent gating is unchanged in spirit (interactive prompt / `--yes` / `auto_dismiss_dialogs`, else clean `SystemExit`); mngr never silently runs an agent on untrusted code.
- Lifecycle hooks now live at the per-agent `$HOME/.gemini/config/hooks.json` and execute directly -- the previous `--add-dir` + `/tmp` hooks-symlink workaround is removed.
- agy's first-run NUX is skipped via a seeded `cache/onboarding.json`. Each agent's `antigravity-oauth-token` is created as a symlink to the shared `~/.gemini/antigravity-cli/antigravity-oauth-token` (default) -- even when that shared token doesn't exist yet. Because agy writes the token in place (verified empirically -- it does not use temp-file + rename), the first agent's login writes *through* the symlink to the shared path, authenticating every other agent and propagating refreshes -- "log in once, anywhere", no manual token handling. This also resolves the spec's open "token-refresh clobbering" risk. `symlink_oauth_token = false` opts into per-agent isolation (copy if a shared token is present, else sign in per agent).
- Path resolution is host-aware (the user's real `$HOME` and OS are resolved on the host in one round-trip), so the token/settings/cache sharing works on remote hosts too. Heavy `ms-playwright-go` browser binaries are shared across agents by symlinking each agent's home cache to the user's real host cache; this (and the oauth-token symlink/copy) is set up at provision time via the shared `imbue.mngr.hosts.common.symlink_or_copy_on_host` helper, so the launch command no longer carries bespoke cache shell.

Auth note: this works on both Linux (no keychain -- the file token is native) and macOS (where agy stores the token in the login keychain, which a relocated per-agent `$HOME` can't reliably read, so the symlinked file token is the cross-agent mechanism there too). On macOS, signing in may surface a harmless system popup -- *"A keychain cannot be found to store \"antigravity.\""* -- because the relocated `$HOME` has no per-agent keychain; agy falls back to the file token and auth completes normally (documented in the README). See the package README.

Internal refactor (no behavior change): the per-agent source-repo trust resolution now delegates to the shared core helper `imbue.mngr.utils.git_utils.find_git_source_path` (extracted from the previously duplicated `mngr_claude` / `mngr_antigravity` methods).

## 2026-06-04

Stopped `antigravity` agents now resume their prior agy conversation on restart, instead of starting a fresh one.

- A `PreInvocation` capture hook records the agent's active agy conversation ID (read from agy's hook payload, which carries `conversationId`) to a per-agent file. On `mngr start`, the launch command resumes the most-recently-active conversation via `agy --conversation <id>`, so the agent keeps its full context across a stop/start. The resume is shell-evaluated at launch (the stored command is replayed on each start) and works under both bash and zsh.
- Resume relies on agy's own incrementally-written conversation store, which survives the hard process kill `mngr stop` performs. If the conversation was pruned, agy warns and starts fresh on its own, so mngr passes `--conversation` whenever an ID is recorded without stat-ing agy's store (keeping the launch command decoupled from agy's on-disk layout).
- Note: agy's `--conversation` only resumes an existing conversation; it cannot mint a caller-supplied ID. mngr therefore lets agy assign the ID and captures it via the hook.

The transcript streamer now discovers this agent's conversation IDs from the same capture-hook file rather than grepping agy's `--log-file`. This is the single source of truth for conversation IDs (shared with resume), and it removes a latent bug where resumed conversations were missed because their log line reads `Resuming conversation` (not the `Resumed conversation` the streamer matched).

Clone-resume (making a cloned antigravity agent continue the source's conversation) is not included here -- agy's conversation store is global rather than per-agent, so it needs separate handling and is left for a follow-up.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

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
