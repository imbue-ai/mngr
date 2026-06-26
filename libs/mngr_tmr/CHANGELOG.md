# Changelog - mngr_tmr

A concise, human-friendly summary of changes for the `mngr_tmr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: TMR agent prompts reworked so generated e2e tests converge to a stable size instead of accreting assertions. The mapper anchors test quality to two sources (claims the tutorial block makes; effects implied by each command or flag), making removal of over-fitted assertions a first-class improvement. Testing agents flag cross-cutting blockers with `# FIXME(tmr): ...` comments. The integrator gained a normalize stage that extracts genuinely-duplicated scaffolding (preserving the 1:1 test/tutorial relationship) and triages FIXMEs, with unresolved blockers surfaced as `escalations` in the outcome schema and HTML report.
- Changed: `mngr tmr` testing agents now publish a single `outputs.tar.gz` archive into the per-agent volume API, replacing the rsync + git-pull finalization; SSH provider no longer supported for testing-agent outputs.
- Changed: TMR run names are now a single compact timestamp `YYYYMMDDHHMMSS` used consistently across the output directory, the `mapreduce_run_name` agent label, and every TMR-spawned entity's agent / host / branch names. Testing agents are `tmr-<run>-<suffix>`, branches `tmr/<run>/<suffix>`, and the random hex id is gone.
- Changed: Added `--run-name` flag to override the auto-generated run name.
- Changed: HTML report is now mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html` (us-west-2) on every regeneration when AWS credentials are set; the public URL `http://go/shared/tmr-reports/<run>.html` is printed and emitted as a structured `report_url` event.
- Changed: Restructured as a thin recipe on top of the new `mngr_mapreduce` framework — all agent launching / polling / extraction code moves out, and TMR is expressed as a `TestMapReduceRecipe` in `imbue.mngr_tmr.recipe`. The `mngr tmr` CLI surface is unchanged for users, but server-side labels are renamed to `mapreduce_role` / `mapreduce_run_name` (the `mapreduce_role` label, valued by `AgentKind`, replaces the previous name-prefix matching used by `--reintegrate`, and `AgentKind` gained a `SNAPSHOTTER` variant) and the outputs-archive path is simplified to `plugin/mapreduce/outputs.tar.gz` — agents from prior TMR runs are not discoverable by this version (drain them with the prior `mngr` build first).
- Changed: Integrator now runs on the same `--provider` as the testing agents and reuses any snapshot they built, so on Modal (or any remote provider) it spins up as quickly as the test agents do instead of running locally; it publishes its results the same way testing agents do (packing `test_output/` + `branch.bundle` into `outputs.tar.gz`) so the orchestrator pulls and applies the integrated branch through the same volume-based path.
- Changed: Unified integrator code path across providers (including local) — the orchestrator rsyncs every testing agent's extracted outputs into `<work_dir>/.mapreduce_inputs/` on the integrator host, and the integrator prompt walks each subdirectory and cherry-picks the qualifying bundles. To make that work, local testing agents now use `GIT_MIRROR` transfer mode instead of `GIT_WORKTREE`, so branches surface in the source repo only via the published bundle (slightly slower locally, but the unified code path makes the local provider a meaningful proxy for the remote one).

### Removed

- Removed: BREAKING — CLI flags `--integrator-provider`, `--integrator-type`, `--integrator-template`, `--integrator-timeout`, and `--use-snapshot` are gone; pass `--provider` once for both testing agents and the integrator, snapshot building is now automatic whenever the provider supports it, and the integrator timeout is now controlled by `--reducer-timeout` (inherited from the `mngr_mapreduce` framework). `--snapshot <ID>` still works for reusing an existing snapshot.

### Fixed

- Fixed: `mngr tmr --provider modal --use-snapshot` now bootstraps the Modal per-user environment on first run instead of aborting with `ProviderEmptyError`; the pre-snapshot provider lookup passes `is_for_host_creation=True` to match the create path.
- Fixed: Several silent-success failure modes now exit non-zero — `--reintegrate` when `mngr list` fails or no agents match the run name, and any tmr run where every test agent failed to launch.
- Fixed: `mngr tmr` See-Also reference now links to `mngr rsync` instead of the removed `pull` command, so the generated docs no longer contain a broken `[mngr help pull](mngr help pull)` markdown link.

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
