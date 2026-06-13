# Changelog - mngr_codex

A concise, human-friendly summary of changes for the `mngr_codex` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub. Each agent runs under its own per-agent `CODEX_HOME` for isolated config, sessions, and transcripts, while sharing authentication through a write-through `auth.json` symlink so logging in once authenticates every agent. Codex agents report RUNNING while working and WAITING when idle via four lifecycle hooks (`UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`) with subagent-aware gating that recomputes the `active` marker under a lock from a root-turn flag plus one file per in-flight subagent (codex's `Stop` fires while subagents are still working with no ordering guarantee on `SubagentStop` and no `fullyIdle` signal). Includes conversation resume across stop/start, common transcripts readable by `mngr transcript`, and seeded trust/onboarding for a silent first launch.
- Added: `send_message` blocks until the agent has actually started processing the submission — the `UserPromptSubmit` hook signals a `mngr-submit-<session>` tmux wait-for channel after it sets the `active` marker, so `mngr message` returns only once the agent reads RUNNING (closes a race where a follow-up lifecycle check could see the pre-turn idle state).
- Added: Codex's blocking startup "Update available!" prompt is disabled (it would intercept the first message); mngr surfaces updates itself at provision via a single `update_policy` setting (default `ASK`: `AUTO` runs `codex update`, `ASK` prompts on an attended local run and otherwise logs a non-blocking notice, `NEVER` only logs). An unattended remote agent provisioned from a local terminal therefore defaults to neither prompting nor upgrading the remote's global install.
