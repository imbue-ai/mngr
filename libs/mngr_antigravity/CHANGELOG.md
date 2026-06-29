# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `update_policy` field on the antigravity agent type (`AUTO` / `ASK` / `NEVER`, default `NEVER`) governing agy's background self-updater via `AGY_CLI_DISABLE_AUTO_UPDATE`. agy has no version-pin capability, so there is no `version` field.

### Changed

- Changed: `settings_overrides` now folds onto the base with a principled merge (matching mngr_claude): a bare key assigns with a recursive narrowing guard, and a top-level `__mngr_merge` map declares per-key `extend` (merge onto the base) or `assign` (replace without the guard). Raw `__extend` / `__assign` suffix keys are rejected — `__mngr_merge` is ignored by vanilla antigravity so the generated `settings.json` stays clean. Previously `settings_overrides` replaced top-level keys wholesale with no guard.

## [v0.1.8] - 2026-06-18

### Added

- Added: Session adoption — `mngr create antigravity --adopt <id>` (or absolute store path) makes a newly created agent resume an existing agy conversation. The conversation is resolved across the user-native agy store, every live local mngr antigravity agent, and every preserved (destroyed) antigravity agent; ambiguous ids are rejected. `--adopt-session` is accepted as an alias. The flag is repeatable (each conversation coexists in the new agent's switcher; the last value is resumed) and may be combined with `--from` (the clone's conversation is the one resumed).
- Added: `--from <agent>` cloning now carries the source agy conversation forward — the clone transfers the source's agy conversation store and resumes its root conversation. If the source has no resumable conversation, the clone warns and starts fresh rather than failing.
- Added: `AntigravityAgent` declares the new capability mixins (`HasSessionPreservationMixin`, `HasUnattendedModeMixin`, `HasPermissionPolicyMixin`, `HasAutoInstallMixin`, `CliBackedAgentMixin`), so these capabilities are code-detectable in the agent capability matrix.
- Added: Auto-install of the `agy` CLI — provisioning installs it (`curl -fsSL https://antigravity.google/cli/install.sh | bash`) when missing, gated by consent on local hosts and the remote-install config flag on remote hosts. New `check_installation` config field (default `True`) disables the check.

### Changed

- Changed: The antigravity common-transcript converter now emits `finish_reason` instead of `stop_reason` on assistant records (aligning with the OpenTelemetry GenAI vocabulary) and a `parts[]` array. antigravity's native format records text and tool calls separately with no relative ordering, so `parts[]` is a best-effort order and `parts_ordered` is false.

### Fixed

- Fixed: antigravity TUI-readiness detection for agy 1.0.9 (which removed the "? for shortcuts" footer hint mngr polled). `mngr message` / `create --message` no longer time out with "Timeout waiting for TUI to be ready" even though agy is up. The readiness signal now matches the input box itself (rule, `>`, rule) via a regex.

## [v0.1.7] - 2026-06-16

### Added

- Added: agy (antigravity) agents now preserve transcripts (raw + common) and conversation-id history on destroy, mirroring the claude plugin. New `preserve_on_destroy` config option (default `true`) — copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`. Works for both online destroys and offline host destruction. agy's native resumable conversation store (`plugin/antigravity/home/.gemini/antigravity-cli/conversations/`) is preserved too, so the agent can be resumed or adopted. Known limitation: on macOS the store is encrypted by the login-keychain "Antigravity Safe Storage" key, so a macOS-created store is not portable to another machine or user.

### Changed

- Changed: Common-transcript converter's event-conversion logic moved out of the inline `python3` heredoc into a standalone `common_transcript_convert.py` (provisioned alongside `common_transcript.sh`), so it is type-checked, linted, and unit-tested directly. Malformed raw-transcript lines, unreadable existing-output lines, non-string USER_INPUT content, and CODE_ACTION records with non-string content are dropped silently rather than crashing the converter.
- Changed: Common-transcript watcher no longer echoes converter errors to the agent's pane — a genuine conversion error is recorded in the structured log only.

### Fixed

- Fixed: Stale `queue_log_path_template=None` kwarg in the antigravity submission path's call to `send_enter_via_tmux_wait_for_hook`; the parameter was removed upstream. agy supplies no acceptance marker so behavior is unchanged, but the plugin now type-checks against the current `tui_utils` signature.

## [v0.1.6] - 2026-06-16

### Changed

- Changed: `statusline.sh` now flushes the transcript pipeline (synchronous `--single-pass` of the raw streamer and common-transcript converter) on the busy->idle edge before clearing the `active` marker, so consumers reading the common transcript on a WAITING transition cannot outrun the converter. The flush and the convert lock come from the shared `mngr_common_transcript_lib.sh` rather than being duplicated per agent.

## [v0.1.5] - 2026-06-15

### Changed

- Changed: Ported the antigravity transcript streamer to agy's new SQLite conversation store (agy 1.0.4+ stopped writing the per-conversation JSONL transcript the old streamer tailed). A new self-contained `decode_agy_transcript.py` reads steps from each `.db` and emits the same record shape, so the common-transcript converter is unchanged; assistant tool calls (name + args) are now decoded too.

### Fixed

- Fixed: On macOS, antigravity (`agy`) agents no longer hang on a modal "A keychain cannot be found to store Antigravity Safe Storage" dialog. Provisioning now symlinks each per-agent home's `Library/Keychains` to the user's real one; Linux is unaffected (Chromium falls back to its file-based store).

## [v0.1.4] - 2026-06-13

### Added

- Added: `agy` alias for the `antigravity` agent type (`mngr create my-agent agy` is equivalent to `mngr create my-agent antigravity`).

### Changed

- Changed: Agent lifecycle replaced the fragile `PreInvocation` / `Stop` marker hooks with a single mngr-owned `statusline.sh` driven by agy's `statusLine`. It maintains the RUNNING/WAITING `active` marker (busy iff `agent_state` is not idle/initializing/authenticating), records the root conversation for resume, and fires the tmux signal confirming submission. A user-provided `statusLine` is composed (mngr runs it with the same payload and emits only its output); a non-runnable command is dropped with a warning.
- Changed: `mngr message` to an antigravity agent now returns only after the agent has started processing the submission (gated on the statusLine signal); the agent reports RUNNING for the whole turn including while subagents run.

## [v0.1.3] - 2026-06-08

### Fixed

- Fixed: Antigravity onboarding seed now also skips agy's first-run NUX for users authenticated through an enterprise account, by marking `enterpriseOnboardingComplete` as `True` (previously `False`); enterprise-authenticated users were getting stuck in the enterprise onboarding flow on their first message.
- Fixed: Passing a model name (or any value containing spaces or parentheses) as an `agy` argument — e.g. `--model "Gemini 3.5 Flash (Medium)"` — no longer breaks `mngr create` with a shell `syntax error near unexpected token '('`. The underlying fix is in `mngr` (`agent_args` are now shell-quoted in `BaseAgent.assemble_command`); this plugin inherits it. The supported `settings_overrides`/`model` path is unaffected.

## [v0.1.2] - 2026-06-05

### Added

- Added: Stopped `antigravity` agents now resume their prior agy conversation on restart. A `PreInvocation` capture hook records the agent's active agy conversation ID to a per-agent file, and `mngr start` shell-evaluates the stored launch command to resume via `agy --conversation <id>`. If the conversation has been pruned, agy starts fresh on its own. Clone-resume is not yet supported because agy's conversation store is global rather than per-agent.
- Added: Per-agent agy isolation — each `antigravity` agent now runs `agy` under its own per-agent `$HOME`, with its own permission policy, model, and isolated config/transcript/session state instead of the previous all-or-nothing `--dangerously-skip-permissions` and shared global `~/.gemini`. Three new agent-type config fields: `settings_overrides` (free-form blob merged into per-agent `settings.json`), `sync_home_settings` (base per-agent settings on a copy of the user's global one; default `true`), and `symlink_oauth_token` (symlink each agent's token to the shared `~/.gemini` token vs copy it; default `true`).
- Added: Shared OAuth token via a write-through symlink to `~/.gemini/antigravity-cli/antigravity-oauth-token` — the first agent's login authenticates every other agent and propagates refreshes ("log in once, anywhere"), resolving the previously open "token-refresh clobbering" risk. `symlink_oauth_token = false` opts into per-agent isolation.

### Changed

- Changed: The transcript streamer now discovers conversation IDs from the same capture-hook file used by resume rather than grepping agy's `--log-file`. This makes the hook file the single source of truth and fixes a latent bug where resumed conversations were missed because their log line reads `Resuming conversation`, not the `Resumed conversation` the streamer matched.
- Changed: Trust now splits by what is persisted — the durable source-repo path goes to the user's global agy settings (no re-prompt across agents/worktrees of the same repo), while the transient per-agent workspace path goes only into the per-agent settings. Consent gating is unchanged in spirit.
- Changed: Lifecycle hooks now live at the per-agent `$HOME/.gemini/config/hooks.json` and execute directly; the previous `--add-dir` + `/tmp` hooks-symlink workaround is removed. agy's first-run NUX is skipped via a seeded `cache/onboarding.json`.
- Changed: Path resolution is host-aware (the user's real `$HOME` and OS are resolved on the host), so token/settings/cache sharing also works on remote hosts. Heavy `ms-playwright-go` browser binaries are now shared across agents by symlinking each agent's home cache to the user's real host cache.

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
