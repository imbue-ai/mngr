# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.2] - 2026-06-05

### Added

- Added: Stopped `antigravity` agents now resume their prior agy conversation on restart. A `PreInvocation` capture hook records the agent's active agy conversation ID to a per-agent file, and `mngr start` shell-evaluates the stored launch command to resume via `agy --conversation <id>`. If the conversation has been pruned, agy starts fresh on its own. Clone-resume is not yet supported because agy's conversation store is global rather than per-agent.
- Added: Per-agent agy isolation — each `antigravity` agent now runs `agy` under its own per-agent `$HOME` at `<agent_state_dir>/plugin/antigravity/home/`, with its own permission policy, model, and isolated config/transcript/session state instead of the previous all-or-nothing `--dangerously-skip-permissions` and shared global `~/.gemini`. Three new agent-type config fields: `settings_overrides` (free-form blob merged into per-agent `settings.json` for `permissions`/`toolPermission`/`model`), `sync_home_settings` (base per-agent `settings.json` on a copy of the user's global one; default `true`), and `symlink_oauth_token` (symlink each agent's token to the shared `~/.gemini` token vs copy it; default `true`).
- Added: Shared OAuth token via a write-through symlink to `~/.gemini/antigravity-cli/antigravity-oauth-token` — the first agent's login authenticates every other agent and propagates refreshes ("log in once, anywhere"), resolving the previously open "token-refresh clobbering" risk. `symlink_oauth_token = false` opts into per-agent isolation.

### Changed

- Changed: The transcript streamer now discovers conversation IDs from the same capture-hook file used by resume rather than grepping agy's `--log-file`. This makes the hook file the single source of truth and fixes a latent bug where resumed conversations were missed because their log line reads `Resuming conversation`, not the `Resumed conversation` the streamer matched.
- Changed: Trust now splits by what is persisted — the durable source-repo path goes to the user's global agy settings (no re-prompt across agents/worktrees of the same repo), while the transient per-agent workspace path goes only into the per-agent settings. Consent gating is unchanged in spirit.
- Changed: Lifecycle hooks now live at the per-agent `$HOME/.gemini/config/hooks.json` and execute directly; the previous `--add-dir` + `/tmp` hooks-symlink workaround is removed. agy's first-run NUX is skipped via a seeded `cache/onboarding.json`.
- Changed: Path resolution is host-aware (the user's real `$HOME` and OS are resolved on the host in one round-trip), so token/settings/cache sharing also works on remote hosts. Heavy `ms-playwright-go` browser binaries are now shared across agents by symlinking each agent's home cache to the user's real host cache, set up at provision time via the new shared `symlink_on_host` / `copy_on_host` helpers in `imbue.mngr.hosts.common`.

### Fixed

- Fixed: `antigravity` agents now stay RUNNING while a subagent or backgrounded task they launched is still working, instead of flipping to WAITING the moment the root agent's turn ends. The `Stop` hook now only clears the `active` lifecycle marker when the payload's conversation id matches the turn's root conversation **and** reports `"fullyIdle":true`; the root is captured by `PreInvocation` and re-recorded at each turn boundary, so `/clear`, `/fork`, `/switch`, and resume stay correct.
- Fixed: `mngr start` on an antigravity agent now resumes the agent's *main* conversation from the captured `root_conversation` rather than the last line of the conversation-ids file, which could be a subagent's conversation. The conversation-ids file is now used only to scope transcript streaming.

## [v0.1.1] - 2026-06-01

### Added

- Added: `antigravity` agent type now uses agy hooks to report lifecycle state. A `PreInvocation` / `Stop` hook pair maintains an `active` marker so antigravity agents now report RUNNING while working and WAITING when idle (previously they had no `active` marker and could not report RUNNING). Verified working against agy 1.0.3.

### Changed

- Changed: mngr now provisions a per-agent `hooks.json` and points agy at it via `--add-dir` (through a `/tmp` symlink, since agy rejects the dotted state-dir path), so the user's global `~/.gemini/config/` is untouched and each agent's state stays isolated.
- Changed: `auto_allow_permissions = true` continues to use the `--dangerously-skip-permissions` CLI flag; agy's documented `PreToolUse` `{"decision": "allow"}` hook output does not actually gate the `run_command` confirmation dialog, so a hook can't replace the flag.

## [v0.1.0] - 2026-05-28

### Added

- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `gemini_config.py` foundation (read/write helpers for `~/.gemini/settings.json`, env-var interpolation, hook builders) plus a `SessionStart` readiness sentinel wired into `GeminiAgent.wait_for_ready_signal`.
- Added: Opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` that installs a `BeforeTool` wildcard hook auto-approving every tool call.
- Added: Gemini agents now emit a common transcript readable by `mngr transcript`; raw gemini session JSONL is captured into `logs/gemini_transcript/events.jsonl`. Opt out with `emit_common_transcript = false`.
- Added: Renamed `mngr_gemini` to `mngr_antigravity`; agent type `gemini` is replaced by `antigravity`. The plugin now targets Google's Antigravity CLI (`agy`), with hook event names that mirror Claude's (`SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Stop`, `Notification`) and `--dangerously-skip-permissions` as the auto-allow flag. Includes Claude-style first-launch trust-folder dismissal via `auto_dismiss_dialogs` (default `False`), and common-transcript scoping per-agent by grepping agy's own log file for `Created conversation <uuid>`.

### Changed

- Changed: `GeminiAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins.
- Changed: `AntigravityAgentConfig.merge_with` follows mngr's new assign-by-default semantics — an override's `cli_args` replaces (rather than concatenates) the base's. Use `cli_args__extend = [...]` for additive layering.
- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.

### Fixed

- Fixed: `antigravity_background_tasks.sh` now uses the `=` exact-match prefix in its `tmux has-session` polling loop so it no longer leaks the transcript streamer and common-transcript converter for stopped agents when a sibling-prefix session is still alive.
