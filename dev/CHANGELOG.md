# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Per-project changelog layout — each `libs/<name>`, `apps/<name>`, and the synthetic top-level `dev/` now owns its own `changelog/` (per-PR entries), `CHANGELOG.md` (concise summary), and `UNABRIDGED_CHANGELOG.md` (verbatim sections). Per-PR entry files live at `<project_dir>/changelog/<branch>.md`, one per project the PR touches.
- Added: New `test_every_project_has_changelog_layout` meta-ratchet enforcing that every project has `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`, and a `changelog/` directory.
- Added: New shared `scripts/changelog_projects.py` owning the path-to-project mapping (used by consolidator, ratchet, and release script).
- Added: A new `TMR (reintegrate)` GitHub Actions workflow takes a run name as required input and runs `mngr tmr --reintegrate <run>`; the main `TMR` workflow accepts a corresponding `run_name` `workflow_dispatch` input. The two TMR workflows share a new `.github/actions/tmr-setup` composite action for common setup steps.
- Added: TMR GitHub Actions workflow reads inbound-SSH authorized keys from a checked-in `.github/tmr-authorized-keys` file (in addition to the existing `additional_authorized_hosts` workflow input); defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace.

### Changed

- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the CI workflow installs.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: Consolidator (`scripts/consolidate_changelog.py`) now walks each project's `<project_dir>/changelog/` and routes entries into `<project_dir>/UNABRIDGED_CHANGELOG.md`; machine-readable output is now one `SECTION <project> <date>` line per inserted section.
- Changed: `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `<project_dir>/changelog/<branch>.md` for each; the consolidation cron's own branch prefix is the only special-cased exemption. Failure message names the resolved diff base and warns about misconfigured / stale bases falsely implicating projects.
- Changed: `scripts/release.py` finalizes each bumped (and first-time-publish) package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section. `apps/<name>/CHANGELOG.md` and `dev/CHANGELOG.md` are not versioned, so their `[Unreleased]` accumulates entries indefinitely.
- Changed: `scripts/release.py` refuses to cut a release when there are unconsolidated entries in `changelog/`; the gate prints the exact one-liner that triggers the `changelog-consolidation` schedule on demand.
- Changed: Offload-acceptance / offload-release runs share a single Modal env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) — justfile recipes pre-create one `mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>` env, forward its name into every sandbox via `--env`, and `trap`-delete it at recipe exit, avoiding the 1500-env-per-workspace cap.
- Changed: TMR GitHub Actions workflow passes AWS secrets through for the S3 report mirror and uses the public URL in the auto-opened PR body, falling back to the existing `tmr-report` artifact when no upload happened.
- Changed: Existing top-level `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files; see each project's `CHANGELOG.md` for its history.

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
