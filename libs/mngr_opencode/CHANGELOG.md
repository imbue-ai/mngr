# Changelog - mngr_opencode

A concise, human-friendly summary of changes to the `mngr_opencode` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Real OpenCode agent-type support. The `opencode` agent graduated from a bare config shell (which ran the binary but reported WAITING forever, with no transcript, resume, or isolation) to a full agent at roughly `mngr_antigravity` parity. Each agent runs as a headless `opencode serve` plus an `opencode attach` TUI client, with lifecycle/transcript maintained by an in-process TypeScript plugin loaded into the server and messages delivered over the server's HTTP API (`prompt_async`). Features:
  - RUNNING vs WAITING lifecycle (subagent-aware: spawning task-tool subagents keeps the agent RUNNING until the whole turn finishes; the marker clear is gated on the root session).
  - Conversation resume across stop/start (`mngr stop` then `mngr start` re-attaches to the prior session via `opencode attach --session <id>`, reading the root id back from per-agent SQLite).
  - Common-transcript and raw-transcript emitted in-process by the plugin -- no background converter or supervisor. Gated by `emit_common_transcript` (default on).
  - Per-agent isolation via `OPENCODE_CONFIG_DIR` and `XDG_DATA_HOME` -- model, permission policy, sessions, and credentials are per-agent and never touch the user's global OpenCode state.
  - Shared auth: by default the per-agent `auth.json` symlinks to `~/.local/share/opencode/auth.json` so a single `opencode auth login` covers every agent (set `symlink_auth = false` for full isolation).
- Added: New `opencode` agent-type config options: `config_overrides` (key/value blob merged last into the per-agent `opencode.json`), `sync_global_config` (default true, base the per-agent config on a copy of the user's `~/.config/opencode/opencode.json`), `symlink_auth` (default true), `auto_allow_permissions` (default false, inject a wildcard allow), `emit_common_transcript` (default true).
- Added: Node-harness conformance test asserting opencode's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`) -- the first CI-runnable check of opencode's in-process TypeScript emitter. Release test runs on the shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`).

## [v0.2.12] - 2026-06-08

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

## [v0.2.9] - 2026-05-28
