# Changelog - mngr_grok

A concise, human-friendly summary of changes for the `mngr_grok` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New package `imbue-mngr-grok` providing the `grok` agent type, which runs xAI's Grok Build CLI (tested with 1.0.62) under `[agent_types.grok]`: hook-driven RUNNING/WAITING lifecycle (background shells and subagents a turn leaves running are held as work leases, reporting `RUNNING_BACKGROUND`; leases hold 24 hours by default, settable with `HOOK_WORK_LEASE_TTL_SECONDS`), confirmed `mngr message` delivery, shared sign-in (`share_auth`), a per-agent `GROK_HOME` isolated from Claude/Cursor config, consented folder trust, session resume/fork/preservation, npm install with optional `version` pinning, and grok's codebase upload and workspace teleport turned off (telemetry follows grok's defaults).
