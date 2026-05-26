# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New direct dependencies recorded in `uv.lock` to support the minds WebDAV file-server mount: `wsgidav` and `a2wsgi`.
- Added: Daily TMR cron at 08:00 UTC via a new `TMR (scheduled)` workflow that gates on a prior periodic PR (`tmr-periodic` label, 4-day window) and invokes the main `TMR` workflow via `workflow_call`; manual `workflow_dispatch` runs are unaffected by the gate.
- Added: `specs/discovery-providers-and-errors/concise.md` describing the cross-project change that promotes per-provider state to first-class fields on `FullDiscoverySnapshotEvent`.
- Added: `specs/minds-env-activate-split/concise.md` describing the split of `minds env activate` into default use-mode and opt-in `--deploy` mode.
- Added: `just minds-test-electron` recipe that wraps the new Electron acceptance test in `xvfb-run -a`; the `test-docker` CI job now installs Node, pnpm, xvfb, and apps/minds pnpm dependencies.
- Added: Auto-generated CLI docs entry at `libs/mngr/docs/commands/secondary/uncapped-claude.md`.
- Added: `.github/actions/tmr-setup` composite action shared between the two TMR workflows; new `TMR (reintegrate)` workflow that runs `mngr tmr --reintegrate <run>` against a previous run name.

### Changed

- Changed: Restructured the changelog system from a single repo-wide changelog to one set of changelog artifacts per project. Per-PR entries now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches); the consolidator routes into each project's `UNABRIDGED_CHANGELOG.md`; `test_pr_has_changelog_entry` ratchet computes the touched projects; a new `test_every_project_has_changelog_layout` meta-ratchet enforces the layout everywhere; `scripts/release.py` finalizes each bumped library's `[Unreleased]` section.
- Changed: `scripts/release.py` now refuses to cut a release when there are unconsolidated entries in any `changelog/`, and prints the exact one-liner that triggers the consolidation schedule on demand.
- Changed: Collapsed Modal environments across `just test-offload-acceptance` / `test-offload-release` runs to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) to stay under the 1500-env-per-workspace cap.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from a checked-in `.github/tmr-authorized-keys` file; AWS secrets are passed through for the S3 report mirror.
- Changed: Default `test_paths` workflow input points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`.
- Changed: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions are available inside agents.
- Changed: README updated to advertise the new `uncapped-claude` command.
- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the CI workflow installs.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).

### Fixed

- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory (`<project_dir>/changelog/`).
- Fixed: TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline, so a failed run is no longer reported as successful when `pipefail` fails to propagate the left-side failure.

## 2026-05-13

### Added

- Added: `mngr_user_id` / `additional_authorized_hosts` `workflow_dispatch` inputs to the TMR GitHub Actions workflow.

### Changed

- Changed: `CHANGELOG.md` is now version-organized — `[Unreleased]` accumulates categorized bullets across cron runs and `scripts/release.py` renames it on each release.
- Changed: Changelog consolidator groups entries by PR-landed committer date (America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md`.
- Changed: Consolidation cron auto-merges `origin/main` before forking the per-run branch, so each PR's diff is just the consolidation commit.
- Changed: TMR GitHub Actions workflow uses the canonical `--format` flag (the previous `--output-format` was not a real option).

## 2026-05-11

### Added

- Added: Per-PR changelog entry system in `changelog/` with nightly consolidation into `UNABRIDGED_CHANGELOG.md` and a version-organized `CHANGELOG.md`; idempotent setup at `scripts/setup_changelog_agent.sh`.
- Added: New meta ratchet `test_every_project_excludes_tests_from_wheel` enforcing a uniform wheel-exclude pattern across every package.

### Changed

- Changed: Upgraded offload from 0.8.1 → 0.9.2 in CI with history-based test scheduling, thin-diff application fix, and propagation of `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` to sandboxes.
- Changed: Workspace wheels uniformly exclude `*_test.py`, `test_*.py`, `**/conftest.py`, `**/testing.py` — previously `libs/mngr` was leaking three test helpers.
- Changed: `scripts/setup_changelog_agent.sh` redeploys when re-run (removes any existing schedule first) and drops the `CHANGELOG_REPLACE=1` gate; the consolidation cron's commit author is now `bot@imbue.com`.

### Fixed

- Fixed: Changelog consolidation cron commit author email corrected from `dev@imbue.com` to `bot@imbue.com` so GitHub attributes commits to the bot account whose token it uses.
