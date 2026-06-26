# Changelog - mngr_codex

A concise, human-friendly summary of changes for the `mngr_codex` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `version` field on the codex agent type pins the installed codex CLI via `npm i -g @openai/codex@<version>`; provisioning verifies the installed codex matches and errors on mismatch. A pin suppresses the provision-time update check (`update_policy` is ignored).

### Changed

- Changed: Codex agent lifecycle detection now targets the agent's primary tmux window by name (`tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index`.

## [v0.1.4] - 2026-06-18

### Added

- Added: Session adoption — `mngr create codex --adopt <id>` (or absolute rollout `.jsonl` path) makes a fresh agent resume an existing codex conversation. Resolved across `~/.codex/sessions`, every live local mngr codex agent, and every preserved (destroyed) codex agent; ambiguous matches are rejected with a clear message. The recorded working directory in the rollout is rewritten to the new agent's work dir, so `codex resume` does not pop the "Choose working directory" modal. The flag is repeatable (each rollout coexists in codex's session switcher; the last one is resumed) and may be combined with `--from`. `--adopt-session` is accepted as an alias.
- Added: `mngr create <new> codex --from <agent>` now resumes the source agent's conversation (transferring the native session store, rebinding the recorded work dir to the clone). If the source has no resumable codex session, the clone warns and continues rather than failing.
- Added: `CodexAgent` declares the new capability mixins (`HasSessionPreservationMixin`, `HasUnattendedModeMixin`, `HasPermissionPolicyMixin`, `HasVersionManagementMixin`, `HasAutoInstallMixin`, `CliBackedAgentMixin`), so these capabilities are code-detectable in the agent capability matrix.
- Added: Auto-install of the `codex` CLI — provisioning installs it (`npm i -g @openai/codex`) when missing, gated by consent on local hosts and the remote-install config flag on remote hosts. New `check_installation` config field (default `True`) disables the check.

### Changed

- Changed: The codex common-transcript converter now records a tool invocation as a nested assistant `tool_calls` entry (sharing the same id as the paired `tool_result`), aligning codex with the other agent ports and the canonical envelope.
- Changed: The codex common-transcript converter now emits `finish_reason` instead of `stop_reason` (aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array on assistant records.

### Fixed

- Fixed: A failure to resolve the user's `CODEX_HOME` during provisioning now surfaces as a clean, user-facing error instead of an abrupt process exit.

## [v0.1.3] - 2026-06-16

### Added

- Added: codex agents now preserve transcripts (raw + common), the root session-id history, and the native resumable rollout session store (`CODEX_HOME/sessions`) on destroy, closing the carried-forward session-preservation gap and matching the claude plugin. New `preserve_on_destroy` config option (default `true`) — copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`. Works for both online destroys and offline host destruction. The auth-token symlink and config sit as siblings in `CODEX_HOME` and are excluded.
- Added: codex background-tasks supervisor launches and supervises an optional usage writer (`codex_usage.sh`) when it's present in the agent's `commands/` dir (installed by the new `imbue-mngr-codex-usage` package), alongside the existing raw/common transcript watchers. No change for agents without the usage plugin installed.

### Changed

- Changed: codex now flushes the common transcript at turn end — when the root turn finishes with no in-flight subagents (the agent goes WAITING), the Stop / SubagentStop hooks run one synchronous `--single-pass` conversion. A consumer harvesting the final message on the WAITING signal no longer races the 5s converter daemon. Matches claude and antigravity.
- Changed: Common-transcript converter's rollout-to-common conversion logic moved out of the inline `python3` heredoc into a standalone `common_transcript_convert.py` (provisioned alongside `common_transcript.sh`), so it is type-checked, linted, and unit-tested directly. Malformed rollout lines and unreadable existing-output lines are dropped silently.
- Changed: Common-transcript watcher no longer echoes converter errors to the agent's pane — a genuine conversion error is recorded in the structured log only.

## [v0.1.2] - 2026-06-16

### Added

- Added: `waiting_reason` field in `mngr list` for codex agents (matching `mngr_claude`): `PERMISSIONS` while blocked on a tool-approval dialog, `END_OF_TURN` when idle. A `PermissionRequest` hook touches a `permissions_waiting` marker (cleared by `PostToolUse` and root `Stop`/`UserPromptSubmit`); the reason is gated on the agent's `active` marker so a stranded marker reports `END_OF_TURN` rather than `PERMISSIONS`. Supervised mode only.

## [v0.1.1] - 2026-06-15

## [v0.1.0] - 2026-06-13

### Added

- Added: real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub. Each agent runs under its own per-agent `CODEX_HOME` for isolated config, sessions, and transcripts, while sharing authentication through a write-through `auth.json` symlink so logging in once authenticates every agent. Codex agents report RUNNING while working and WAITING when idle via four hooks (`UserPromptSubmit`/`Stop`/`SubagentStart`/`SubagentStop`); because codex subagents run asynchronously, the `active` marker is recomputed under a lock from a root-turn flag plus one file per in-flight subagent so it stays RUNNING until the root turn AND every subagent are done. Includes conversation resume across stop/start (root `session_id` captured to a tracking file; `mngr start` shell-evaluates `codex resume <id>`), common transcripts readable by `mngr transcript`, and seeded trust/onboarding for a silent first launch.
- Added: `send_message` blocks until submission registers — the `UserPromptSubmit` hook signals a `mngr-submit-<session>` tmux wait-for channel after it sets the active marker, so `mngr message` returns only once the agent reads RUNNING (closes a race where a follow-up lifecycle check could see the pre-turn idle state).
- Added: Update handling — codex's blocking startup "Update available!" prompt is disabled, and mngr surfaces updates itself at provision by comparing `codex --version` to `~/.codex/version.json`. A single `update_policy` setting governs action (default `ASK`): `AUTO` runs `codex update` silently, `ASK` prompts on an attended local run (interactive tty + local host) and otherwise logs a non-blocking notice, and `NEVER` only logs the notice.
- Added: Conformance test asserting codex's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`); release test now runs on the shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`) against the real codex binary, including stop/start resume.
