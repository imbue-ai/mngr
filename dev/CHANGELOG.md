# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `blueprint/claude-stream-buffer/plan-claude-stream-buffer.md` — design plan for approximate Claude response streaming via the mngr tmux session (implemented in `imbue-mngr-claude` and `imbue-mngr-robinhood`).
- Added: New direct dependencies recorded in `uv.lock` to support the minds WebDAV file-server mount: `wsgidav` and `a2wsgi`.
- Added: Daily TMR cron at 08:00 UTC via a new `TMR (scheduled)` workflow that gates on a prior periodic PR (`tmr-periodic` label, 4-day window) and invokes the main `TMR` workflow via `workflow_call`; manual `workflow_dispatch` runs are unaffected by the gate.
- Added: `just minds-test-electron` recipe that wraps the new Electron acceptance test in `xvfb-run -a`; the `test-docker` CI job now installs Node, pnpm, xvfb, and apps/minds pnpm dependencies.
- Added: `.github/actions/tmr-setup` composite action shared between the two TMR workflows; new `TMR (reintegrate)` workflow that runs `mngr tmr --reintegrate <run>` against a previous run name.
- Added: `scripts/make_cli_docs.py` gained a `--check` mode that reports stale generated CLI docs (and the exact regen command) and exits non-zero without writing; a new `test_cli_docs_are_up_to_date` meta-ratchet runs `--check` so committed CLI docs and the PyPI README cannot drift from the generator output.
- Added: `specs/env-settings-overrides/concise.md` documenting the new `MNGR__*` env-var override scheme, the `__extend` operator, and the assign-by-default merge semantics.
- Added: Nightly changelog consolidation now runs a per-project accuracy review, spawning reviewer subagents that verify the generated bullets against the actual code and commit corrections.
- Added: Root `pyproject.toml` gains `[tool.uv] exclude-newer` to enforce the supply-chain cooldown at lock time (`uv lock` refuses any package version newer than the cutoff); `scripts/release.py` advances the cutoff forward-only to `(today_utc - 2 weeks)` at each release.
- Added: `uv-sync-pre-push` hook in `.pre-commit-config.yaml` (ordered first at the `pre-push` stage) that runs `uv sync --all-packages` whenever the push touches `uv.lock` or any `pyproject.toml`, so subsequent `uv run`-based hooks (`ruff`, `ty`, `regenerate-cli-docs`, `compile-style-guide`) don't `ModuleNotFoundError` on a freshly-merged new workspace member.
- Added: `ty` pre-push hook in `.pre-commit-config.yaml` that runs `uv run ty check` over the whole workspace (ty can't scope to staged files), restoring a type-check gate now that the per-project `test_no_type_errors` tests were consolidated repo-wide.
- Added: `scripts/snapshot_minds_e2e_state.py`, a Modal-sandbox script that boots the desktop client and snapshots the running workspace; the resulting image ID can be passed to offload via `--override-image-id` to boot future e2e runs from an already-warm workspace in seconds.
- Added: New self-hosted-macOS-runner CI workflows: `minds-launch-to-msg.yml` (builds or reuses a ToDesktop `.app`, launches it on the `minds-runner` mac, and optionally round-trips a real first-message chat against a LIMA agent) and `minds-runner-reset.yml` (manual runner cleanup).
- Added: New design / implementation specs under `specs/` covering docker cleanup, imbue-cloud R2 buckets, minds/host backups, and several VPS docker volume layouts.
- Added: Updated `.minds/template/cloudflare.sh` secret template documenting that `CLOUDFLARE_API_TOKEN` must now be an account-owned token with R2 storage permissions, and that R2 must be enabled on the Cloudflare account.
- Added: `markdown-it-py` is now an explicit (rather than only transitive) dependency in the lockfile; mngr uses rich's CommonMark parser directly to rewrite links when rendering help topics for the terminal.
- Added: `specs/discovery-provider-error-resilience.md` documenting two remaining discovery-resilience loose threads from the workspace-flicker debugging.
- Added: New design / implementation blueprints under `blueprint/` for two-tier minds workspace recovery, the error-hierarchy collapse, paid-user DB tables, and imbue_cloud fast/slow-path host-leasing.
- Added: New `audit-ci` Claude skill for auditing recent CI runs for anomalies (warnings, uncached/rebuilt docker images, flaky/slow tests, regressions), including notes on the repo's CI layout to avoid common false positives.
- Added: New blueprint plans under `blueprint/` for the apps/minds JinjaX template migration and disabling OVH-side VPS qemu backups.
- Added: New dedicated `.github/workflows/release-tests.yml` workflow that runs the release tests on `workflow_dispatch` (trigger against `main` to validate a commit before cutting a release) and on `v*` tag pushes (a backstop record). `scripts/release.py` now prints an advisory warning before the release confirmation prompt if the Release Tests workflow has not passed on the exact commit being tagged.
- Added: Dev `mngr` shim (`scripts/mngr`) so `mngr` always runs the checkout you're working in (per-worktree, by cwd) instead of a stale global install. A pre-commit hook (`scripts/check_mngr_shim.sh`) installs the shim automatically (symlink in `~/.local/bin`) and verifies it's on PATH — no per-worktree setup. README dev-install notes updated to use the shim instead of `uv tool install -e libs/mngr`.
- Added: New `test_every_mngr_plugin_isolates_home_in_tests` meta-ratchet — every mngr plugin (any project with a `[project.entry-points.mngr]` table) must call `register_plugin_test_fixtures(globals())` in a conftest, guaranteeing its tests redirect $HOME away from the developer's real home directory.
- Added: New blueprints / design docs under `blueprint/` covering the inbox modal refactor, minds create-flow fixes, gvisor docker hardening, the LIMA docker host, the mngr agent SDK, and tmux window sizing.
- Added: New `just test-sdk-live` recipe that sets `RUN_SDK_LIVE_TESTS=1` and runs the `sdk_live`-marked live Claude Agent SDK tests in `libs/mngr_robinhood`.
- Added: New blueprint plans under `blueprint/` for the titlebar workspace-accent rework, the startup loading-window position fix, the leaked Docker state-container investigation, and the create-template `setting` / `setting__extend` fix.

### Changed

- Changed: `just minds-start` and `just minds-build` now select the Node version pinned in `apps/minds/.nvmrc` (via nvm) before launching, so they no longer fail with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default Node has drifted off the pin. It never auto-installs Node, erroring with an actionable hint when nvm or the pinned version is missing.
- Changed: `just forward-system-interface` now writes the Cloudflare tunnel token to `runtime/secrets/cloudflare_tunnel.env` (one of the per-secret env files in the `runtime/secrets/` directory) instead of the old single `runtime/secrets` file, matching the directory-based secrets layout the FCT runner and minds now use.
- Changed: Removed `.minds/template/paid-accounts.sh` and folded `MINDS_PAID_ADMIN_KEY` + `MINDS_PAID_LIST_CACHE_TTL_SECONDS` into `.minds/template/supertokens.sh`, reflecting the move of paid-user tracking from a Modal-secret allowlist to database tables. The vault-environments spec's service list is updated accordingly.
- Changed: Updated the root `.minds/template/ovh.sh` secret template comment to note that the OVH AK/AS/CK credentials are now pushed to Modal (as the `ovh-<env>` secret) for the connector's runtime cleanup of released pool hosts, not just read on the operator's machine during deploy/destroy.
- Changed: Fixed stale references in the `minds-dev-workflow` skill and the `minds-start` justfile error hints — dev env naming corrected from `<your-user>-dev` to `dev-<your-user>` (the validator requires the tier prefix first), along with the corresponding derived paths and worktree base-branch example.
- Changed: The `/sync-tutorial-to-e2e-tests` skill's default test-directory argument now points at the new `libs/mngr/imbue/mngr/e2e/tutorial/` subdirectory, so it no longer flags non-tutorial e2e tests as unmatched.
- Changed: Updated `CLAUDE.md` per-PR changelog-writing guidance — when a per-PR changelog entry uses a list, its bullets should be separated by a double newline (a blank line between each bullet).
- Changed: `scripts/snapshot_minds_e2e_state.py` now sets `LATCHKEY_DISABLE_COUNTING=1` in the in-sandbox runner before booting minds, so the snapshot builder (test infrastructure) does not count toward Latchkey's usage. Genuine minds installs (including dev-from-source launches via `just minds-start`) intentionally still count.

- Changed: Restructured the changelog system from a single repo-wide changelog to one set of changelog artifacts per project. Per-PR entries now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches), and the consolidator routes each into the project's own `UNABRIDGED_CHANGELOG.md`. Ratchets enforce the per-project layout and require an entry per touched project.
- Changed: `scripts/release.py` now refuses to cut a release when there are unconsolidated entries in any `changelog/`, and prints the exact one-liner that triggers the consolidation schedule on demand.
- Changed: Collapsed Modal environments across `just test-offload-acceptance` / `test-offload-release` runs to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) to stay under the 1500-env-per-workspace cap.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from a checked-in `.github/tmr-authorized-keys` file; AWS secrets are passed through for the S3 report mirror.
- Changed: Default `test_paths` workflow input points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`.
- Changed: `CLAUDE.local.md` is now copied into agent workdirs by default so user-specific Claude instructions are available inside agents.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing.
- Changed: Nightly changelog consolidation prompt now treats each project's `CHANGELOG.md` as a notable-only summary — non-notable changes (canonically, test-only changes) are omitted from `CHANGELOG.md` entirely instead of being forced into a `Changed` bullet. Such entries are still preserved verbatim in each project's `UNABRIDGED_CHANGELOG.md`. Added a `dev`-project exception that judges `dev` entries by developer/maintainer impact.
- Changed: Workspace and scripts metadata follows the `libs/mngr_gemini` → `libs/mngr_antigravity` rename.
- Changed: Spec docs updated to note that agents now reach the Minds API exclusively through the latchkey gateway (the per-agent `MINDS_API_KEY` and reverse SSH tunnel are gone), and that the electron acceptance test uses `launch_mode=DOCKER`.
- Changed: Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear improvement and doesn't change behavior (e.g. inline re-construction folded into a pre-existing local).
- Changed: Bumped the `test-docker-electron` CI job to Node.js `24.15.0` and pnpm `10.33.4` to match the exact-version pins now in `apps/minds/package.json`, and refreshed the electron-desktop-app spec to match the real packaged files.
- Changed: Bumped the offload CI pin from `0.9.5` to `0.9.7` in `.github/workflows/ci.yml` (v0.9.6 adds `offload run --override-image-id <ID>`, Modal-only).
- Changed: Bumped the `ty` type checker floor from `0.0.24` to `0.0.39` (root `pyproject.toml`). 0.0.39 no longer honors bracketed PEP-484 `# type: ignore[<code>]` suppressions; all such comments in the repo were converted to `# ty: ignore[<ty-rule>]`.
- Changed: Bumped pinned dependencies via `uv lock --upgrade` under the two-week cooldown, including major bumps to `paramiko` (3→4), `coolname` (3→5), `starlette` (0.50→1.0), `urwid` (3→4), and others.
- Changed: Consolidated `test_no_type_errors` and `test_no_ruff_errors` to run once repo-wide from `test_meta_ratchets.py`, removing ~36 redundant per-project copies (each was a full ~0.8s cold workspace scan with no cross-process cache benefit).
- Changed: Documented in `CLAUDE.md` (Ratchets section) how to tighten a ratchet count after reducing violations: `uv run pytest --inline-snapshot=trim <test_ratchets.py>` (only `=trim` lowers a count that already passes; `=fix`/`=update` do not).
- Changed: Retired the hand-written git-hook installer — deleted `scripts/githooks/install.sh` and `scripts/githooks/pre-commit`, and updated `scripts/ruff-precommit-setup-guide.md` to install via `uv run pre-commit install` instead. The symlink installer only ever installed the `pre-commit` hook, never the `pre-push` or `post-checkout` hooks; `pre-commit install` installs every hook type in `default_install_hook_types`.
- Changed: Removed the stale `MNGR_ALLOW_PYTEST` reference from `specs/env-settings-overrides/concise.md`, following the env-var's removal from mngr.
- Changed: Added `libs/mngr_mapreduce` to the workspace; the root `pyproject.toml` now collects coverage for `imbue.mngr_mapreduce`.
- Changed: Dropped the now-removed `--use-snapshot` flag from `.github/workflows/tmr.yml` so scheduled / manual TMR runs don't fail at invocation (snapshot building on `--provider modal` is automatic now), and refreshed the stale `--use-snapshot` comment in `.github/workflows/tmr-reintegrate.yml`.
- Changed: Spec file-tree listings under `specs/electron-desktop-app/` (`concise.md` + `spec.md`) now show `todesktop.js` instead of `todesktop.json`, tracking the apps/minds rename.
- Changed: Sped up the `test-offload` and `test-offload-acceptance` checkouts by unshallowing only the current ref instead of fetching the full history of every branch, which could add minutes per run on a repo with many branches.
- Changed: Removed the dead "release" branch apparatus from CI. There is no `release` branch (releases are cut from `main` as `v*` tags), so the old `test-release` / `test-docker-release` jobs that gated on it never ran; `ci.yml` no longer references the release branch and the release-test jobs moved to the new `release-tests.yml` workflow.
- Changed: Bumped all GitHub Actions pinned to deprecated Node.js-20 runtimes to their latest Node.js-24 majors, removing the deprecation warnings from CI logs.
- Changed: Updated the repo-root local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`) to expose the full current Anthropic Claude lineup with inline per-token pricing, kept in sync with `apps/modal_litellm/app.py` by a drift test.
- Changed: `scripts/install.sh` now invokes the reworked dependencies command as `mngr dependencies --install interactive --scope core` (was `mngr dependencies -i`), so a missing optional dependency (`ssh`/`rsync`/`unison`/`claude`) no longer trips the installer warning — only missing core dependencies (`git`/`tmux`/`jq`) do.
- Changed: Updated root-level references for the `mngr_uncapped_claude` → `mngr_robinhood` plugin rename (README, root `pyproject.toml` coverage, CLI docs, spec directory, and `uv.lock`).
- Changed: Release tooling — added several previously-missing mngr plugins to the publish graph so they are version-bumped, pin-aligned, and offered for first publication by `scripts/release.py`.
- Changed: Release tooling now auto-discovers the publish graph from the workspace instead of using a hand-maintained allowlist: every `libs/*` package is a publish candidate unless explicitly listed in `UNPUBLISHED_PACKAGES`. Previously a package nobody remembered to add to the hardcoded list was invisible to the release script (never bumped, pin-aligned, or offered). A new ratchet guarantees every `libs/*` package is either published or explicitly unpublished.
- Changed: Internal-dependency pin alignment now walks every workspace member's dependencies, optional extras, and dependency groups — not just published packages' main dependencies — and a stale or missing internal pin now fails `test_internal_dep_pins_are_consistent` in CI.
- Changed: New-package detection now considers the full release candidate set (directly-changed packages plus everything pulled in by the cascade and the mngr-always rule), not just directly-changed packages. An unpublished package reached only via cascade is now correctly offered for first publication instead of being silently bumped and published as if it already existed.
- Changed: The `imbue-mngr-skills` Claude Code plugin is now published from its own GitHub repo as a plugin marketplace (mirroring `imbue-code-guardian`), and this repo dogfoods the published plugin instead of carrying the skills in its project-level `.claude/skills/` directory.
- Changed: `just minds-start` now exports `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` alongside the `MINDS_WORKSPACE_*` vars — the explicit opt-in that makes the minds desktop create-form honor the local-worktree defaults on any tier (including staging/production), instead of only on per-developer dev envs.
- Changed: Excluded the new opt-in live Claude Agent SDK test suite from CI by adding `and not sdk_live` to both pytest filter expressions in `offload-modal.toml`.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).
- Removed: `test_no_dependencies_younger_than_two_weeks` (and its `_FRESHNESS_EXEMPT_PACKAGES` / `_lock_package_upload_time` helpers) from `test_meta_ratchets.py`; the cooldown is now enforced at lock time via `[tool.uv] exclude-newer`, so the time-relative test is redundant.

### Fixed

- Fixed: TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline, so a failed run is no longer reported as successful when `pipefail` fails to propagate the left-side failure.
- Fixed: Tightened the `test_every_project_has_changelog_layout` meta-ratchet to also require a `.gitkeep` inside each project's `changelog/` directory. Previously only the directory's existence was checked, so a newly added project with no `.gitkeep` would pass until a later consolidation run drained its entries and the empty directory silently vanished from git.
- Fixed: `mngr-shim-installed` pre-commit hook no longer gives a false failure when invoked under `uv run` (e.g. during `mngr create`), where the project-local `mngr` console script shadowed the dev shim. The hook now resolves `mngr` the way an interactive shell would, while still catching a genuinely stale global.
- Fixed: Added a `**/tmr-report/` pattern to the root `.gitignore` so the test-orchestrator run-report directory is no longer flagged as an untracked change (the existing `**/tmr_*/` pattern used an underscore and did not match the dash-named directory).
- Fixed: `publish` workflow's "Verify versions and pin consistency" step now uses `uv run --all-packages` so the workspace `imbue.mngr` package is installed, avoiding a `ModuleNotFoundError` on the `UNPUBLISHED_PACKAGES` import.

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
