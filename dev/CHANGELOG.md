# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: TMR GitHub Actions workflow now runs on a daily cron at 08:00 UTC via a new `TMR (scheduled)` wrapper workflow that gates on a prior periodic PR (`tmr-periodic` label) and invokes the main `TMR` workflow via `workflow_call`; auto-opened PRs are labeled `tmr-periodic` and assigned to `qi-imbue` and `joshalbrecht`.
- Added: `just minds-test-electron` recipe wrapping `test_create_local_docker_workspace_via_electron` in `xvfb-run -a`; the existing `test-docker` CI job now installs Node, pnpm, xvfb, and the minds pnpm dependencies so the Electron binary is available.
- Added: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions from the host repo are available inside agents.
- Added: New `TMR (reintegrate)` workflow taking a `run_name` input and running `mngr tmr --reintegrate <run>`; a shared `.github/actions/tmr-setup` composite action covers common setup between the two TMR workflows.
- Added: `specs/minds-env-activate-split/concise.md` design for splitting `minds env activate` into use-mode (default) and `--deploy` mode.

### Changed

- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the CI workflow installs.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: Restructured the changelog system from a single repo-wide changelog into per-project artifacts — each project (`libs/<name>`, `apps/<name>`, plus the synthetic top-level `dev/`) now owns its own `changelog/`, `CHANGELOG.md`, and `UNABRIDGED_CHANGELOG.md` at its root; the consolidator routes entries per project and emits `SECTION <project> <date>` lines; a new `test_every_project_has_changelog_layout` meta-ratchet enforces the layout. `scripts/release.py` finalizes each bumped package's `CHANGELOG.md` `[Unreleased]`.
- Changed: TMR workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from a checked-in `.github/tmr-authorized-keys` file; the workflow passes AWS secrets through for the S3 report mirror and uses the public URL in the auto-opened PR body.
- Changed: Default TMR `test_paths` workflow input now points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`.
- Changed: Modal envs can be collapsed across offload-acceptance / offload-release runs to a single shared env via `MNGR_TEST_SHARED_MODAL_ENV_NAME`; `just test-offload-{acceptance,release}` pre-create one env and `trap`-delete it at recipe exit.
- Changed: `scripts/release.py` now refuses to cut a release when there are unconsolidated entries in any `<project_dir>/changelog/`, printing the on-demand one-liner that triggers the consolidation schedule.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).

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
