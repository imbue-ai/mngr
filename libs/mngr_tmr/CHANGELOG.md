# Changelog - mngr_tmr

A concise, human-friendly summary of changes for the `mngr_tmr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `--run-name` flag on `mngr tmr` to override the auto-generated run name.
- Added: TMR HTML report is mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html` (us-west-2) when `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are set; public URL printed and emitted as a structured `report_url` event.

### Changed

- Changed: `mngr tmr` testing agents now publish a single `outputs.tar.gz` archive into the per-agent volume API, replacing the rsync + git-pull finalization; SSH provider no longer supported for testing-agent outputs.
- Changed: Regenerated CLI docs for `mngr tmr`.
- Changed: TMR run names are now a single compact timestamp `YYYYMMDDHHMMSS` used consistently across output directory, `tmr_run_name` agent label, and the agent / host / branch names of every TMR-spawned entity. New `tmr_role` label (`testing` / `snapshotter` / `integrator`) replaces name-prefix matching for filtering integrator agents during `--reintegrate`.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `mngr tmr --additional-authorized-host` repeatable flag for installing SSH public keys on every agent host.

### Changed

- Changed: `mngr tmr --use-snapshot` no longer re-uploads the code repo per test agent — agents source from on-host `/code` via `git-worktree`.

## [v0.2.7] - 2026-05-11

### Added

- Added: TMR `integrator_branch` event on `mngr tmr`'s structured stdout (`--format jsonl`/`json`).

### Changed

- Changed: `mngr tmr` HTML reports gain a dedicated "Failed" section separate from "Blocked" (infrastructure failures vs. agent-reported BLOCKED), and now include rows for launch-failed agents.

### Fixed

- Fixed: `mngr tmr` no longer crashes the whole orchestrator when a single agent fails its initial-message send — launching loops also catch `AgentError`, and failed launches render as errored entries in HTML reports.
- Fixed: `mngr tmr` integrator launch (and any local-provider test-agent launch) no longer always fails with "Failed to generate a unique host name after 100 attempts"; TMR now reuses the existing local host when the provider is `local`.
