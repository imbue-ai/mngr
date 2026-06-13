# Changelog - mngr_pi_coding

A concise, human-friendly summary of changes to the `mngr_pi_coding` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `pi` alias for the `pi-coding` agent type — `mngr create my-agent pi` is now equivalent to `mngr create my-agent pi-coding`.
- Added: Real lifecycle parity for `pi-coding` agents. A single mngr-owned pi extension (loaded via `pi -e`) drives RUNNING vs WAITING reporting on `agent_start`/`agent_end` events (subagent-aware: stays correct when an agent spawns a nested `pi` via its bash tool), `mngr transcript`, and conversation resume across `mngr stop` / `mngr start` with full context. Agent creation waits on a real readiness sentinel the extension writes when pi's session loads (rather than scraping the startup banner). Messages are delivered via `pi.sendUserMessage` (an inbox file the extension's watcher injects) instead of simulating tmux keystrokes — behaves identically on local and remote hosts (the old paste+Enter path intermittently swallowed the first Enter). On remote hosts (when allowed), `pi` is auto-installed from npm; on local hosts it still defers to the user unless `--yes` is passed. Pi 0.79+'s "Trust project folder?" dialog is pre-trusted (seeding pi's `trust.json`), gated like the claude/antigravity agent types — silent under `mngr create --yes` or the new `auto_dismiss_dialogs` config, interactive prompt otherwise. New config: `emit_common_transcript`, `emit_raw_transcript`, `resume_session` (all default on).

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

### Fixed

- Fixed: Remote provisioning of pi resource directories (skills/prompts/extensions/themes) now transfers with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file and did not scale to large resource sets (the same failure mode as github issue 1825).

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Changed

- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.
