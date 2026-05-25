# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Per-project changelog layout — each project under `libs/`, `apps/`, plus a synthetic top-level `dev/` directory, owns its own `changelog/` entries dir, `CHANGELOG.md`, and `UNABRIDGED_CHANGELOG.md`. The `test_pr_has_changelog_entry` ratchet now computes touched projects from the PR diff and requires `<project_dir>/changelog/<branch>.md` for each. New `test_every_project_has_changelog_layout` meta-ratchet enforces the layout.
- Added: New shared `scripts/changelog_projects.py` owns the path-to-project mapping (used by the consolidator, the ratchet, and the release script).
- Added: TMR daily cron workflow at 08:00 UTC with a `tmr-periodic` label gate; auto-opened PRs are labelled and assigned to `qi-imbue` and `joshalbrecht`. Default `test_paths` now points at the whole `libs/mngr/imbue/mngr/e2e/` directory.
- Added: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions from the host repo are available inside agents.
- Added: `just minds-test-electron` recipe wraps the new Electron acceptance test in `xvfb-run -a`; the `test-docker` CI job now installs Node, pnpm, xvfb, and apps/minds pnpm deps.
- Added: `wsgidav` and `a2wsgi` direct dependencies in `uv.lock` to support the minds WebDAV file-server mount.
- Added: Specs `specs/discovery-providers-and-errors/concise.md` and `specs/minds-env-activate-split/concise.md`.
- Added: Auto-generated CLI docs entry at `libs/mngr/docs/commands/secondary/uncapped-claude.md` so `mngr ask` and `mngr --help` know about the new `mngr_uncapped_claude` command.

### Changed

- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the CI workflow installs.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: Per-PR entry files now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches), instead of a single repo-root `changelog/<branch>.md`. Consolidator's machine-readable output is now one `SECTION <project> <date>` line per inserted section.
- Changed: `scripts/release.py` finalizes each bumped package's and each first-time-publish package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section. Releases now refuse to cut when unconsolidated entries remain, printing the exact one-liner that triggers the consolidation schedule on demand.
- Changed: Modal offload-acceptance/release runs now share a single Modal env (`mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>`) pre-created and `trap`-deleted by the justfile recipes; opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`.
- Changed: TMR GitHub Actions defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH keys from `.github/tmr-authorized-keys`; passes AWS secrets through for the S3 report mirror.
- Changed: TMR `run_name` workflow_dispatch input; new `TMR (reintegrate)` workflow re-runs just the integrator phase against a prior run name.
- Changed: README updated to advertise the new `uncapped-claude` command and link to the new sub-project.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).
- Removed: The top-level repo-wide `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files.

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
