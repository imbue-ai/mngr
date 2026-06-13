# Changelog - mngr_opencode

A concise, human-friendly summary of changes to the `mngr_opencode` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Real OpenCode agent-type support. The `opencode` agent type graduated from a bare config shell (which ran the binary but reported WAITING forever, with no transcript, resume, or isolation) to a full agent at roughly `mngr_antigravity` parity. OpenCode is architecturally unlike Claude Code / Antigravity (a client-server app with SQLite-backed sessions and no POSIX-sh hook mechanism), so each agent runs as a headless `opencode serve` plus an `opencode attach` TUI client, with an in-process TypeScript plugin loaded into the server driving lifecycle and transcripts; messages go over the server's HTTP API. Features: RUNNING vs WAITING lifecycle with subagent-aware gating (spawning task-tool subagents keeps the agent RUNNING until the whole turn finishes), conversation resume across `mngr stop` / `mngr start` via the recorded root session id (`opencode attach --session <id>`), `mngr transcript` support (both raw and common-format transcripts written in-process; gated by `emit_common_transcript`, default on), and per-agent isolation (each agent gets its own `OPENCODE_CONFIG_DIR` and `XDG_DATA_HOME`).

## [v0.2.12] - 2026-06-08

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

## [v0.2.9] - 2026-05-28
