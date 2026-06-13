# Changelog - mngr_codex

A concise, human-friendly summary of changes for the `mngr_codex` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub. Each agent runs under its own per-agent `CODEX_HOME` for isolated config, sessions, and transcripts, while sharing authentication through a write-through `auth.json` symlink so logging in once authenticates every agent. Codex agents report RUNNING while working and WAITING when idle via four hooks (`UserPromptSubmit`/`Stop`/`SubagentStart`/`SubagentStop`); because codex subagents run asynchronously, the `active` marker is recomputed under a lock from a root-turn flag plus one file per in-flight subagent so it stays RUNNING until the root turn AND every subagent are done. Includes conversation resume across stop/start (root `session_id` captured to a tracking file; `mngr start` shell-evaluates `codex resume <id>`), common transcripts readable by `mngr transcript`, and seeded trust/onboarding for a silent first launch.
- Added: `send_message` blocks until submission registers — the `UserPromptSubmit` hook signals a `mngr-submit-<session>` tmux wait-for channel after it sets the active marker, so `mngr message` returns only once the agent reads RUNNING (closes a race where a follow-up lifecycle check could see the pre-turn idle state).
- Added: Update handling — codex's blocking startup "Update available!" prompt is disabled, and mngr surfaces updates itself at provision by comparing `codex --version` to `~/.codex/version.json`. A single `update_policy` setting governs action (default `ASK`): `AUTO` runs `codex update` silently, `ASK` prompts on an attended local run (interactive tty + local host) and otherwise logs a non-blocking notice, and `NEVER` only logs the notice.
- Added: Conformance test asserting codex's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`); release test now runs on the shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`) against the real codex binary, including stop/start resume.
