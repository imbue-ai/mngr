# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: TMR GitHub Actions workflow runs on a daily cron at 08:00 UTC via a new `TMR (scheduled)` workflow that gates on a prior periodic PR (`tmr-periodic` label); auto-opened PRs are assigned to `qi-imbue` and `joshalbrecht`. Manual `workflow_dispatch` runs remain independent of the gate.
- Added: `TMR (reintegrate)` workflow that takes a run name and re-runs `mngr tmr --reintegrate <run>`.
- Added: TMR workflows now share a `.github/actions/tmr-setup` composite action; default `test_paths` widened to the whole `libs/mngr/imbue/mngr/e2e/` directory.
- Added: `just minds-test-electron` recipe that wraps the new Electron acceptance test in `xvfb-run -a`; the `test-docker` CI job installs Node, pnpm, xvfb, and `apps/minds` pnpm dependencies so the Electron binary is available.
- Added: Per-project changelog layout — each project under `libs/`, `apps/`, plus a synthetic top-level `dev/` now holds `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at its root; the consolidator routes entries per project and the ratchet computes per-project changelog-entry requirements.
- Added: `test_every_project_has_changelog_layout` meta-ratchet enforcing each project has its three changelog artifacts.
- Added: Shared `scripts/changelog_projects.py` owning the path-to-project mapping used by the consolidator, the ratchet, and the release script.
- Added: Spec `specs/discovery-providers-and-errors/concise.md` describing the cross-project promotion of per-provider state and the new UNKNOWN agent/host lifecycle.
- Added: Spec `specs/minds-env-activate-split/concise.md` for splitting `minds env activate` into a default use-mode and an opt-in `--deploy` mode.
- Added: `wsgidav` + `a2wsgi` as new direct dependencies in `uv.lock` (for the minds WebDAV file-server mount).
- Added: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions are available inside agents.
- Added: `mngr_uncapped_claude` top-level documentation entries — README link and auto-generated CLI docs at `libs/mngr/docs/commands/secondary/uncapped-claude.md`.

### Changed

- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the CI workflow installs.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: `scripts/release.py` refuses to cut a release when there are unconsolidated entries in `changelog/`; prints the on-demand consolidation trigger one-liner. Predicate lives next to the consolidator in `scripts/consolidate_changelog.py`.
- Changed: `scripts/release.py` finalizes each bumped package's and each first-time-publish package's `[Unreleased]` section.
- Changed: Offload-acceptance / offload-release runs share a single Modal env via `MNGR_TEST_SHARED_MODAL_ENV_NAME`; the justfile recipes pre-create one `mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>` env per run and trap-delete on exit.
- Changed: `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `<project_dir>/changelog/<branch>.md` for each; failure message names the resolved diff base.
- Changed: TMR GitHub Actions workflow now defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from `.github/tmr-authorized-keys`; passes AWS secrets through for the S3 report mirror.

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
