# Changelog - mngr_grok

A concise, human-friendly summary of changes for the `mngr_grok` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New package `imbue-mngr-grok` providing the `grok` agent type, which runs xAI's Grok Build CLI (tested with 1.0.62) under `[agent_types.grok]`: hook-driven RUNNING/WAITING lifecycle (background shells and subagents a turn leaves running are recorded under `background_tasks/` with their `kind` and grok's own `native_type`, reporting `RUNNING_BACKGROUND`; a record has no deadline and stays until grok no longer lists the task), confirmed `mngr message` delivery, shared sign-in (`share_auth`), a per-agent `GROK_HOME` isolated from Claude/Cursor config, consented folder trust, session resume/fork/preservation, npm install with optional `version` pinning, and grok's codebase upload and workspace teleport turned off (telemetry follows grok's defaults).
- Added: grok agents record `turn_end` for the latest root prompt: `COMPLETED` at `turn_completed{end_turn}`, `FAILED` at `StopFailure`, `INTERRUPTED` at `StopCancelled`.
- Added: `mngr upgrade` support: rewrites mngr's `commands/` scripts, shell libraries, `hooks/mngr.json` and hook handler, then restarts an idle agent and resumes its session; agents not launched via `commands/grok_launch.sh` are skipped until `mngr create NAME --reuse --update`.
