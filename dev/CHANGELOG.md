# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New direct dependencies recorded in `uv.lock` to support the minds WebDAV file-server mount: `wsgidav` and `a2wsgi`.
- Added: Daily TMR cron at 08:00 UTC via a new `TMR (scheduled)` workflow that gates on a prior periodic PR (`tmr-periodic` label, 4-day window) and invokes the main `TMR` workflow via `workflow_call`; manual `workflow_dispatch` runs are unaffected by the gate.
- Added: `just minds-test-electron` recipe that wraps the new Electron acceptance test in `xvfb-run -a`; the `test-docker` CI job now installs Node, pnpm, xvfb, and apps/minds pnpm dependencies.
- Added: `.github/actions/tmr-setup` composite action shared between the two TMR workflows; new `TMR (reintegrate)` workflow that runs `mngr tmr --reintegrate <run>` against a previous run name.
- Added: `scripts/make_cli_docs.py` gained a `--check` mode that reports stale generated CLI docs (and the exact regen command) and exits non-zero without writing; a new `test_cli_docs_are_up_to_date` meta-ratchet runs `--check` so committed CLI docs and the PyPI README cannot drift from the generator output.
- Added: `specs/env-settings-overrides/concise.md` documenting the new `MNGR__*` env-var override scheme, the `__extend` operator, and the assign-by-default merge semantics.
- Added: `[tool.uv] exclude-newer` in the root `pyproject.toml` enforces a two-week supply-chain dependency cooldown at lock time (initial cutoff `2026-05-23T00:00:00Z`). `scripts/release.py` advances the cutoff forward-only at each release (`max(current, release_date - 2 weeks)`), committing the root `pyproject.toml` alongside the version bumps.
- Added: `libs/mngr_mapreduce` to the uv workspace; root `pyproject.toml` now collects coverage for `imbue.mngr_mapreduce`.
- Added: `scripts/snapshot_minds_e2e_state.py` demonstration script that captures a Modal sandbox state with a warm minds workspace + Docker container (using `experimental_options={"vm_runtime": True}` + `sandbox.snapshot_filesystem()`), so future test runs can boot from a pre-built Modal image via offload's new `--override-image-id` instead of rebuilding from scratch.
- Added: `ty` pre-push hook in `.pre-commit-config.yaml` running `uv run ty check` over the whole workspace, so scoped local runs (`just test-quick libs/<project>`) still get a type-check gate after the consolidation.

### Changed

- Changed: Restructured the changelog system from a single repo-wide changelog to one set of changelog artifacts per project. Per-PR entries now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches); the consolidator routes into each project's `UNABRIDGED_CHANGELOG.md`; `test_pr_has_changelog_entry` ratchet computes the touched projects; a new `test_every_project_has_changelog_layout` meta-ratchet enforces the layout everywhere; `scripts/release.py` finalizes each bumped library's `[Unreleased]` section.
- Changed: `scripts/release.py` now refuses to cut a release when there are unconsolidated entries in any `changelog/`, and prints the exact one-liner that triggers the consolidation schedule on demand.
- Changed: Collapsed Modal environments across `just test-offload-acceptance` / `test-offload-release` runs to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) to stay under the 1500-env-per-workspace cap.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from a checked-in `.github/tmr-authorized-keys` file; AWS secrets are passed through for the S3 report mirror.
- Changed: Default `test_paths` workflow input points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`.
- Changed: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions are available inside agents.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: Nightly changelog consolidation prompt now treats each project's `CHANGELOG.md` as a notable-only summary — non-notable changes (canonically, test-only changes) are omitted from `CHANGELOG.md` entirely instead of being forced into a `Changed` bullet. Such entries are still preserved verbatim in each project's `UNABRIDGED_CHANGELOG.md`. Added a `dev`-project exception that judges `dev` entries by developer/maintainer impact.
- Changed: Workspace + scripts metadata (workspace `pyproject.toml` cov target, `test_profiles.toml` mngr-suite test paths, top-level `README.md`, `scripts/utils.py` package list) follows the `libs/mngr_gemini` → `libs/mngr_antigravity` rename.
- Changed: `specs/minds-rest-api/spec.md` got a top-of-file banner noting that the per-agent `MINDS_API_KEY` and the per-agent reverse SSH tunnel for the Minds API are both gone (agents now reach the API exclusively through the latchkey gateway's `minds-api-proxy` extension); `specs/minds-electron-acceptance-test/spec.md` now references `launch_mode=DOCKER` instead of `LOCAL`.
- Changed: Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear improvement and doesn't change behavior (e.g. inline re-construction folded into a pre-existing local).
- Changed: Consolidated per-project `test_no_type_errors` and `test_no_ruff_errors` (~36 copies, one per workspace member) into a single repo-wide pair in `test_meta_ratchets.py`. Each duplicate invocation was a full ~0.8s cold workspace scan with no cross-process cache benefit.
- Changed: Bumped the `test-docker-electron` CI job's Node.js to 24.15.0 and pnpm to 10.33.4 to match the new exact-version pins in `apps/minds/package.json`; refreshed `specs/electron-desktop-app/spec.md` so the example `pyproject.toml` block matches the real packaged file.
- Changed: Bumped the offload CI pin from 0.9.5 to 0.9.6 in `.github/workflows/ci.yml`; 0.9.6 adds `offload run --override-image-id <ID>` (Modal provider only) for skipping image setup.
- Changed: Raised the `ty` type-checker floor from 0.0.24 to 0.0.39 in the root `pyproject.toml`; bumped pinned `paramiko` 3.5.1 → 4.0.0 and `coolname` 3.0.0 → 5.0.0 in `uv.lock` (paramiko pulls `pyinfra` 3.6.1 → 3.8.0 and adds `invoke` / `types-paramiko` transitively). All bracketed `# type: ignore[...]` suppressions converted to `# ty: ignore[...]` (ty 0.0.39 no longer honors the bracketed mypy-style form).
- Changed: Bumped many floating dependencies under the two-week supply-chain cooldown via `uv lock --upgrade`. Notable bumps within the window: `starlette` 0.50 → 1.0, `urwid` 3.0 → 4.0, `pydantic` 2.12 → 2.13, `cryptography` 46 → 48, `typer` 0.21 → 0.25, `uvicorn` 0.40 → 0.46.
- Changed: Tightened recorded ratchet violation counts to their current exact values across all projects via `--inline-snapshot=trim`, locking in previously-unrecorded reductions. Documented the workflow in `CLAUDE.md`.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).
- Removed: `test_no_dependencies_younger_than_two_weeks` meta-ratchet (and its `_FRESHNESS_EXEMPT_PACKAGES` / `_lock_package_upload_time` helpers) — uv now enforces the cooldown at lock time via `[tool.uv] exclude-newer`, so the test is redundant.
- Removed: Stale `MNGR_ALLOW_PYTEST` reference from `specs/env-settings-overrides/concise.md` (the env var was removed from mngr).

### Fixed

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
