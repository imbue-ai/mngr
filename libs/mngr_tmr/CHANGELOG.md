# Changelog - mngr_tmr

A concise, human-friendly summary of changes for the `mngr_tmr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr tmr` testing agents now publish a single `outputs.tar.gz` archive into the per-agent volume API, replacing the rsync + git-pull finalization; SSH provider no longer supported for testing-agent outputs.
- Changed: TMR run names are now a single compact timestamp `YYYYMMDDHHMMSS` used consistently across the output directory, the `tmr_run_name` agent label, and every TMR-spawned entity's agent / host / branch names. Testing agents are `tmr-<run>-<test_name>`, branches `mngr-tmr/<run>/<test_name>`, and the random hex id is gone.
- Changed: Added `--run-name` flag to override the auto-generated run name.
- Changed: HTML report is now mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html` (us-west-2) on every regeneration when AWS credentials are set; the public URL `http://go/shared/tmr-reports/<run>.html` is printed and emitted as a structured `report_url` event.
- Changed: `tmr_role` agent label (`testing` / `snapshotter` / `integrator`) replaces the previous name-prefix matching for filtering integrator agents during `--reintegrate`; derived directly from `AgentKind` which gained a `SNAPSHOTTER` variant.

### Fixed

- Fixed: `mngr tmr --provider modal --use-snapshot` now bootstraps the Modal per-user environment on first run instead of aborting with `ProviderEmptyError`; the pre-snapshot provider lookup passes `is_for_host_creation=True` to match the create path.
- Fixed: Several silent-success failure modes now exit non-zero — `--reintegrate` when `mngr list` fails or no agents match the run name, and any tmr run where every test agent failed to launch.

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
