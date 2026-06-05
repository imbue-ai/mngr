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
- Added: Nightly changelog consolidation now runs a per-project accuracy review of the bullets it just generated. After the consolidation commit, the agent spawns one or more fresh-context `general-purpose` reviewer subagents (spec at `scripts/changelog_accuracy_reviewer.md`) partitioned across the projects with new bullets, runs them in parallel, and each subagent verifies its assigned projects' bullets against the actual code and commits corrections (touching only its assigned `CHANGELOG.md` files).
- Added: Root `pyproject.toml` gains `[tool.uv] exclude-newer = "<date>"` to enforce the supply-chain cooldown at lock time (`uv lock` refuses any package version uploaded after the cutoff); `scripts/release.py` advances the cutoff forward-only at each release to `(today_utc - 2 weeks)`, leaving an already-younger cutoff untouched.
- Added: `uv-sync-pre-push` hook in `.pre-commit-config.yaml` (ordered first at the `pre-push` stage) that runs `uv sync --all-packages` whenever the push touches `uv.lock` or any `pyproject.toml`, so subsequent `uv run`-based hooks (`ruff`, `ty`, `regenerate-cli-docs`, `compile-style-guide`) don't `ModuleNotFoundError` on a freshly-merged new workspace member.
- Added: `ty` pre-push hook in `.pre-commit-config.yaml` that runs `uv run ty check` over the whole workspace (ty can't scope to staged files), restoring a type-check gate now that the per-project `test_no_type_errors` tests were consolidated repo-wide.
- Added: `scripts/snapshot_minds_e2e_state.py`, a Modal-sandbox demonstration script that boots the desktop client through `imbue.minds.desktop_client.e2e_workspace_runner.create_workspace_via_electron`, deliberately skips `mngr destroy` so the workspace agent + Docker container survive, and calls `sandbox.snapshot_filesystem()` — the resulting Modal image ID can be fed to offload via `--override-image-id` to boot future e2e runs from an already-warm workspace in seconds.
- Added: New self-hosted-macOS-runner CI: `.github/workflows/minds-launch-to-msg.yml` (workflow_dispatch job that takes a minds commit SHA + forever-claude-template ref, either reuses an existing ToDesktop build matching the commit or runs `pnpm dist` to build a fresh draft, then on the `minds-runner` mac downloads the resulting `.app`, launches it, waits for the backend, optionally round-trips a real first-message chat against a LIMA agent, and collects diagnostic artifacts on failure) and `.github/workflows/minds-runner-reset.yml` (manual runner cleanup, optionally installing a fresh `.app` from a ToDesktop `.zip` URL). The runner is registered at the `imbue-ai` org level and targeted via `runs-on: [self-hosted, macOS, minds-runner]`.
- Added: New design / implementation specs under `specs/`: `docker-cleanup-state-and-images/`, `imbue-cloud-r2-buckets/spec.md`, `minds-backup-provider/concise.md`, `host-backup/concise.md`, `symlink-code-onto-mngr-volume/concise.md`, `vps-docker-btrfs/concise.md`, and `vps-docker-unified-volume/concise.md`.
- Added: Updated `.minds/template/cloudflare.sh` secret template documenting that `CLOUDFLARE_API_TOKEN` must now be an account-owned (`cfat_`) token with `Workers R2 Storage: Edit` + `Account API Tokens: Edit` (on top of the existing tunnel/DNS/Access/KV permissions), and that R2 must be enabled on the Cloudflare account.
- Added: `markdown-it-py` is now an explicit (rather than only transitive) dependency in the lockfile; mngr uses rich's CommonMark parser directly to rewrite links when rendering help topics for the terminal.
- Added: `specs/discovery-provider-error-resilience.md` documenting the two remaining discovery-resilience loose threads from the workspace-flicker debugging — retaining known hosts/agents through a transient provider discovery error, and bouncing/restarting the latchkey forward on the same triggers minds uses for its own observe.
- Added: New design / implementation blueprints under `blueprint/` — `tiered-restart-v2/plan-tiered-restart-v2.md` (two-tier minds workspace recovery flow and the `mngr stop --stop-host` flag), the error-hierarchy collapse plan, `paid-user-tables/` (implementation blueprint backing the move from a Modal-secret allowlist to DB tables), and `imbue-cloud-slow-path/` (imbue_cloud robust fast/slow-path host-leasing change).
- Added: New `audit-ci` Claude skill (`.claude/skills/audit-ci/SKILL.md`) documenting how to audit recent CI runs for anomalies (warnings, uncached/rebuilt docker images, flaky/slow tests, regressions). Explains the repo's counterintuitive CI layout (test results live in separately-synthesized `Unit + Integration Tests` / `Acceptance Tests` check-runs shown as "in 0s" rather than in the workflow jobs) and includes calibration notes to avoid common false positives.
- Added: New blueprint plans under `blueprint/` — `jinjax-migration/` (apps/minds template migration to JinjaX) and `disable-ovh-qemu-backups/` (disabling OVH-side VPS backups by purging qemu at the OVH provider level).
- Added: New dedicated `.github/workflows/release-tests.yml` workflow that runs the release tests on `workflow_dispatch` (trigger against `main` to validate a commit before cutting a release) and on `v*` tag pushes (a backstop record). `scripts/release.py` now prints an advisory warning before the release confirmation prompt if the Release Tests workflow has not passed on the exact commit being tagged.

### Changed

- Changed: `just minds-start` and `just minds-build` now select the Node version pinned in `apps/minds/.nvmrc` (via nvm) before launching, so they no longer fail with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default Node has drifted off the pin. The selection is a no-op when the active Node already matches and errors with an actionable hint when nvm or the pinned version is missing (it never auto-installs Node). Shared with `propagate_changes` via the new `apps/minds/scripts/select_node_version.sh` helper.
- Changed: `just forward-system-interface` now writes the Cloudflare tunnel token to `runtime/secrets/cloudflare_tunnel.env` (one of the per-secret env files in the `runtime/secrets/` directory) instead of the old single `runtime/secrets` file, matching the directory-based secrets layout the FCT runner and minds now use.
- Changed: Removed `.minds/template/paid-accounts.sh` and folded `MINDS_PAID_ADMIN_KEY` + `MINDS_PAID_LIST_CACHE_TTL_SECONDS` into `.minds/template/supertokens.sh`, reflecting the move of paid-user tracking from a Modal-secret allowlist to database tables. The vault-environments spec's service list is updated accordingly.
- Changed: Updated the root `.minds/template/ovh.sh` secret template comment to note that the OVH AK/AS/CK credentials are now pushed to Modal (as the `ovh-<env>` secret) for the connector's runtime cleanup of released pool hosts, not just read on the operator's machine during deploy/destroy.
- Changed: Fixed stale references in the `minds-dev-workflow` skill and the `minds-start` justfile error hints — dev env naming corrected from `<your-user>-dev` to `dev-<your-user>` (the `DevEnvName` validator requires the tier prefix first); derived paths corrected (`MINDS_ROOT_NAME=minds-dev-<user>`, env root `~/.minds-dev-<user>/`, container `minds-dev-<user>-mindtest-host`); worktree base branch example replaced with `origin/main`; pool-host baking described as OVH-backed.
- Changed: The `/sync-tutorial-to-e2e-tests` skill's default test-directory argument now points at the new `libs/mngr/imbue/mngr/e2e/tutorial/` subdirectory, so it no longer flags non-tutorial e2e tests as unmatched.

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
- Changed: Bumped `test-docker-electron` CI job to Node.js `24.15.0` and pnpm `10.33.4` to match the exact-version pins now in `apps/minds/package.json`. Refreshed the example `pyproject.toml` block in `specs/electron-desktop-app/spec.md` to match the real packaged file (`requires-python = "==3.12.13"`, the actual three-dependency list); corrected the standalone-pyproject path reference from `electron/pyproject.toml` to `electron/pyproject/pyproject.toml`.
- Changed: Bumped the offload CI pin from `0.9.5` to `0.9.7` in `.github/workflows/ci.yml` (v0.9.6 adds `offload run --override-image-id <ID>`, Modal-only).
- Changed: Bumped the `ty` type checker floor from `0.0.24` to `0.0.39` (root `pyproject.toml`). 0.0.39 no longer honors bracketed PEP-484 `# type: ignore[<code>]` suppressions; all such comments in the repo were converted to `# ty: ignore[<ty-rule>]`.
- Changed: Bumped pinned dependencies via `uv lock --upgrade` under the two-week cooldown: `paramiko` 3.5.1 → 4.0.0 (capped at <5 by pyinfra), `coolname` 3.0.0 → 5.0.0, plus floating bumps including `starlette` 0.50 → 1.0, `urwid` 3.0 → 4.0, `pydantic` 2.12 → 2.13, `cryptography` 46 → 48, `typer` 0.21 → 0.25, `uvicorn` 0.40 → 0.46. The paramiko bump also pulls `pyinfra` 3.6.1 → 3.8.0 and adds `invoke` + `types-paramiko` transitively.
- Changed: Consolidated `test_no_type_errors` and `test_no_ruff_errors` to run once repo-wide from `test_meta_ratchets.py`, removing ~36 redundant per-project copies (each was a full ~0.8s cold workspace scan with no cross-process cache benefit).
- Changed: Documented in `CLAUDE.md` (Ratchets section) how to tighten a ratchet count after reducing violations: `uv run pytest --inline-snapshot=trim <test_ratchets.py>` (only `=trim` lowers a count that already passes; `=fix`/`=update` do not).
- Changed: Retired the hand-written git-hook installer — deleted `scripts/githooks/install.sh` and `scripts/githooks/pre-commit`, and updated `scripts/ruff-precommit-setup-guide.md` to install via `uv run pre-commit install` instead. The symlink installer only ever installed the `pre-commit` hook, never the `pre-push` or `post-checkout` hooks; `pre-commit install` installs every hook type in `default_install_hook_types`.
- Changed: Removed the stale `MNGR_ALLOW_PYTEST` reference from `specs/env-settings-overrides/concise.md`, following the env-var's removal from mngr.
- Changed: Added `libs/mngr_mapreduce` to the workspace; the root `pyproject.toml` now collects coverage for `imbue.mngr_mapreduce`.
- Changed: Dropped the now-removed `--use-snapshot` flag from `.github/workflows/tmr.yml` so scheduled / manual TMR runs don't fail at invocation (snapshot building on `--provider modal` is automatic now), and refreshed the stale `--use-snapshot` comment in `.github/workflows/tmr-reintegrate.yml`.
- Changed: Spec file-tree listings under `specs/electron-desktop-app/` (`concise.md` + `spec.md`) now show `todesktop.js` instead of `todesktop.json`, tracking the apps/minds rename.
- Changed: Speed up the `test-offload` and `test-offload-acceptance` checkouts: instead of `fetch-depth: 0` (which fetches the full history of *every* branch), do a default shallow checkout and then `git fetch --unshallow` only the current ref. Offload needs the full ancestry of HEAD to find its checkpoint commit and thin-diff against it, but not other branches; on a repo with many branches the all-branches fetch can add minutes to each run.
- Changed: Removed the dead "release" branch apparatus from CI. There is no `release` branch — releases are cut from `main` as `v*` tags — so the old `test-release` / `test-docker-release` jobs (gated to `refs/heads/release` push) never ran. `ci.yml` no longer references the release branch (dropped the `release` push trigger and the four `github.ref != 'refs/heads/release'` job guards); the two release-test jobs move to the new `release-tests.yml` workflow. Refreshed the stale "Release Tests" description in `style_guide.md` and dropped the dead `release` branch from the changelog-ratchet PR-branch skip in `test_meta_ratchets.py`.
- Changed: Bumped GitHub Actions pinned to Node.js-20 runtimes (deprecated by GitHub; forced to Node 24 starting 2026-06-16) to their latest Node.js-24 majors: `actions/cache` v4→v5, `actions/upload-artifact` v4→v7, `actions/setup-node` v4→v6, `actions/checkout` v4→v6 (`vet.yml`), `extractions/setup-just` v2→v4, `mikepenz/action-junit-report` v5→v6, and `astral-sh/setup-uv` v6→v7. Removes the Node.js-20 deprecation warnings from CI logs.
- Changed: Updated the repo-root local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`) to expose the full current Anthropic Claude lineup (Opus 4.8/4.7/4.6/4.5/4.1, Sonnet 4.6/4.5, Haiku 4.5, plus the dated Opus 4 / Sonnet 4 ids) with inline per-token pricing. Kept in sync with `apps/modal_litellm/app.py` by a drift test.
- Changed: `scripts/install.sh` now invokes the reworked dependencies command as `mngr dependencies --install interactive --scope core` (was `mngr dependencies -i`), so a missing optional dependency (`ssh`/`rsync`/`unison`/`claude`) no longer trips the installer warning — only missing core dependencies (`git`/`tmux`/`jq`) do.
- Changed: Updated root-level references for the `mngr_uncapped_claude` → `mngr_robinhood` plugin rename: top-level `README.md` sub-projects list, `--cov=imbue.mngr_robinhood` in root `pyproject.toml`, the `robinhood` entry in `scripts/make_cli_docs.py`'s secondary-command set, the `specs/robinhood/` spec directory, and `uv.lock`.
- Changed: Release tooling (`scripts/utils.py`) — added `imbue-mngr-usage`, `imbue-mngr-claude-usage`, `imbue-mngr-forward`, `imbue-mngr-latchkey`, `imbue-mngr-imbue-cloud`, `imbue-mngr-ovh`, `imbue-mngr-schedule`, and `imbue-mngr-robinhood` to the hard-coded `PACKAGES` publish graph so they are version-bumped, pin-aligned, and offered for first publication by `scripts/release.py`. Their internal dependency pins were realigned to current workspace versions to satisfy `test_internal_dep_pins_are_consistent`.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).
- Removed: `test_no_dependencies_younger_than_two_weeks` (and its `_FRESHNESS_EXEMPT_PACKAGES` / `_lock_package_upload_time` helpers) from `test_meta_ratchets.py`; the cooldown is now enforced at lock time via `[tool.uv] exclude-newer`, so the time-relative test is redundant.

### Fixed

- Fixed: TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline, so a failed run is no longer reported as successful when `pipefail` fails to propagate the left-side failure.
- Fixed: Tightened the `test_every_project_has_changelog_layout` meta-ratchet to also require a `.gitkeep` inside each project's `changelog/` directory. Previously only the directory's existence was checked, so a newly added project with no `.gitkeep` would pass until a later consolidation run drained its entries and the empty directory silently vanished from git.

### Security

- Security: Upgraded two vulnerable transitive dependencies in `uv.lock` to their fixed versions (surfaced by `uv audit`): `idna` 3.14→3.16 and `starlette` 1.0.0→1.0.1.

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
