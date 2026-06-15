# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-06-13

### Added

- Added: `aws` create-template in `.mngr/settings.toml` for dogfooding the codebase on AWS EC2 -- `mngr create -t aws <name>` builds the dev Dockerfile and runs an agent on EC2. The shared `[providers.aws]` config (`us-west-2`, `t3.large`, `auto_shutdown_minutes = 120`, `builder = "DEPOT"`) is committed; `DEPOT` requires exporting `DEPOT_TOKEN` and `GH_TOKEN`.
- Added: New design specs and blueprints under `specs/` and `blueprint/` (agent-plugin parity, env-settings overrides, discovery error-resilience, two-tier workspace recovery, host / imbue-cloud backups, R2 buckets, docker cleanup and gvisor hardening, the workspace color picker, the JinjaX template migration, the agent SDK, and more).
- Added: New Claude skills -- `minds-justfile` (routes minds tasks through the root justfile), `audit-ci` (audits recent CI runs for anomalies), and `identify-bad-tests` (flags low-quality / fragile tests into the library's `_tasks/`).
- Added: Changelog / release justfile recipes -- `just release` wraps `scripts/release.py`, `just changelog-deploy` (re)deploys the nightly consolidation schedule, and `just changelog-trigger` runs consolidation on demand. Plus pool-host recipes `just bake-pool-host` / `list-pool-hosts` / `destroy-pool-host` wrapping the env-aware `minds pool` CLI.
- Added: Nightly changelog consolidation now runs a per-project accuracy review, spawning reviewer subagents that verify the generated bullets against the actual code and commit corrections.
- Added: Supply-chain cooldown enforced at lock time -- root `pyproject.toml` `[tool.uv] exclude-newer` makes `uv lock` refuse any package newer than the cutoff, which `scripts/release.py` advances forward-only to `(today_utc - 2 weeks)` each release.
- Added: Pre-push hooks in `.pre-commit-config.yaml` -- `uv-sync-pre-push` runs `uv sync --all-packages` when a push touches `uv.lock` / `pyproject.toml` (so later `uv run` hooks don't `ModuleNotFoundError` on a new workspace member), and a `ty` hook runs `uv run ty check` workspace-wide.
- Added: `scripts/make_cli_docs.py --check` mode reports stale generated CLI docs and exits non-zero; a new `test_cli_docs_are_up_to_date` meta-ratchet runs it so committed CLI docs / the PyPI README cannot drift from the generator.
- Added: Dev `mngr` shim (`scripts/mngr`) so `mngr` always runs the checkout you're working in (per-worktree, by cwd) instead of a stale global install; a pre-commit hook (`scripts/check_mngr_shim.sh`) installs and verifies it automatically.
- Added: New meta-ratchet `test_every_mngr_plugin_isolates_home_in_tests` -- every mngr plugin must call `register_plugin_test_fixtures(globals())` in a conftest so its tests redirect `$HOME` away from the developer's real home.
- Added: `scripts/snapshot_minds_e2e_state.py`, a Modal-sandbox script that boots the desktop client and snapshots the running workspace; the resulting image ID can be passed to offload via `--override-image-id` to boot e2e runs from an already-warm workspace.
- Added: New CI workflows -- `release-tests.yml` (runs release tests on `workflow_dispatch` / `v*` tags, with `release.py` warning if it has not passed on the commit being tagged), self-hosted-macOS `minds-launch-to-msg.yml` (+ `minds-runner-reset.yml`), and a daily TMR cron plus a `TMR (reintegrate)` workflow sharing a `tmr-setup` composite action.
- Added: `just minds-test-electron` recipe (wraps the Electron acceptance test in `xvfb-run`; the `test-docker` job now installs Node / pnpm / xvfb) and `just test-sdk-live` (runs the `sdk_live`-marked live Claude Agent SDK tests).
- Added: Twice-daily `minds launch-to-first-message` schedule that builds and verifies current mngr `main` against FCT `main` with the full slack flow, surfacing drift before each workday.
- Added: Updated `.minds/template/cloudflare.sh` to document that `CLOUDFLARE_API_TOKEN` must now be an account-owned token with R2 storage permissions.
- Added: Design plan under `blueprint/host-backup-snapshot-rotation/` for fixing empty gVisor host backups -- unique time-named btrfs snapshots, keep-newest-N retention, and exit-code-only backup failure signaling.

### Changed

- Changed: Restructured the changelog consolidation prompt (`scripts/changelog_consolidation_prompt.md`) for more concise summaries -- concise `CHANGELOG.md` bullets are generated once per project (not once per date, which created cross-date duplicates), followed by a critical concision pass that drops non-notable bullets; the merge step also drops `Fixed` entries for bugs introduced and fixed within the release window.
- Changed: Nightly consolidation now treats each `CHANGELOG.md` as a notable-only summary -- non-notable (canonically, test-only) changes are omitted entirely (still kept verbatim in `UNABRIDGED_CHANGELOG.md`), with a `dev`-project exception judged by developer / maintainer impact.
- Changed: Restructured the changelog system to one set of artifacts per project -- per-PR entries live at `<project_dir>/changelog/<branch>.md` and the consolidator routes each into the project's own `UNABRIDGED_CHANGELOG.md`; ratchets enforce the layout.
- Changed: Renamed the changelog tooling scripts to share a `changelog_` prefix (`changelog_consolidate.py`, `changelog_schedule_utils.py`, `changelog_deploy.sh`); `changelog_deploy.sh` now reads `GH_TOKEN` / `ANTHROPIC_API_KEY` from Vault at deploy time (`vault login -method=oidc` first).
- Changed: `scripts/release.py` now refuses to cut a release when any `changelog/` has unconsolidated entries, and points users at `just changelog-trigger` to run the consolidation on demand.
- Changed: Release tooling now auto-discovers the publish graph from the workspace (every `libs/*` package is a candidate unless listed in `UNPUBLISHED_PACKAGES`, enforced by a ratchet), walks every member's deps / extras / groups for internal-pin alignment (`test_internal_dep_pins_are_consistent`), and considers the full release-candidate cascade when offering new packages for first publication.
- Changed: `imbue-mngr-skills` is now published from its own GitHub repo as a plugin marketplace (mirroring `imbue-code-guardian`); this repo dogfoods the published plugin instead of carrying the skills in `.claude/skills/`.
- Changed: AWS provider root-level changes -- regenerated `mngr create` CLI docs (new AWS build-args help, dropped the Vultr / OVH `--vps-os=` line, per-provider prefix renames), added `aws` to `make_cli_docs.py` `SECONDARY_COMMANDS` and to top-level coverage, and added the six new AWS deps to `uv.lock`.
- Changed: Scripts (`release.py`, `modal_nuke.py`, `make_cli_docs.py`, `sync_common_ratchets.py`, `warm_cli_example.py`) no longer swallow unexpected failures -- PyPI lookups raise on network / HTTP errors, Modal identifiers come from documented `--json` keys (raising `ModalSchemaError` on schema drift), and several silent-fallback bugs now raise instead.
- Changed: `minds-launch-to-msg.yml` consolidated -- build moved to the self-hosted `minds-runner` Mac (so the `.app` ships Mac-native `uv` / `git` / `lima`), `commit_sha` made required (no stale-bundle escape hatch), the renamed `minds-macos-launch.yml` smoke folded in as a parallel job, and the shell-script + slack-mock pipeline rewritten as one Python script (`apps/minds/scripts/launch_to_msg_e2e.py`). Screenshots now ride only per-run GitHub artifacts (the ~1.2 GB `ci-screenshots` orphan branch was retired), with per-window Playwright captures as the headline shots.
- Changed: CI speedups -- acceptance wall-clock cut ~62% (`contents: write` for the image-cache git-notes push, `max_parallel` 200→50 for better LPT packing); `test-offload` / `test-offload-acceptance` unshallow only the current ref; and the dead `release`-branch jobs were removed (release tests moved to `release-tests.yml`).
- Changed: Consolidated `test_no_type_errors` / `test_no_ruff_errors` to run once repo-wide from `test_meta_ratchets.py`, removing ~36 redundant per-project copies.
- Changed: Retired the hand-written git-hook installer (`scripts/githooks/`) in favor of `uv run pre-commit install`, which installs every hook type (the old symlink installer only ever installed `pre-commit`).
- Changed: Dependency bumps under the two-week cooldown -- `ty` floor `0.0.24`→`0.0.39` (which no longer honors `# type: ignore[<code>]`; all were converted to `# ty: ignore[<rule>]`), majors via `uv lock --upgrade` (`paramiko` 3→4, `coolname` 3→5, `starlette` 0.50→1.0, `urwid` 3→4), the offload CI pin `0.9.5`→`0.9.7`, and Node-24 runtimes for `test-docker-electron` and all GitHub Actions.
- Changed: Tracked plugin renames / additions in root config -- `mngr_gemini`→`mngr_antigravity`, `mngr_uncapped_claude`→`mngr_robinhood`, and added `libs/mngr_mapreduce` to the workspace (+ coverage).
- Changed: Documented in `CLAUDE.md` -- release tests do not run in CI (must be run locally), per-PR changelog list bullets need a blank line between them, and how to tighten a ratchet count (`pytest --inline-snapshot=trim`). `CLAUDE.local.md` is now copied into agent workdirs so user-specific instructions are available inside agents.
- Changed: Skill fixes -- corrected stale dev-env naming in the `minds-dev-workflow` skill and `minds-start` hints (`dev-<user>`, tier prefix first), pointed `sync-tutorial-to-e2e-tests` at the new `e2e/tutorial/` dir, and removed the contradictory "commit when finished" notes from the `identify-*` skills.
- Changed: `just minds-start` selects the `.nvmrc`-pinned Node before launching (erroring with a hint if nvm / the version is missing, never auto-installing) and exports `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` so the create-form honors local-worktree defaults on any tier.
- Changed: `just forward-system-interface` writes the Cloudflare tunnel token to `runtime/secrets/cloudflare_tunnel.env`, matching the directory-based secrets layout; removed `.minds/template/paid-accounts.sh`, folding `MINDS_PAID_ADMIN_KEY` / `MINDS_PAID_LIST_CACHE_TTL_SECONDS` into `supertokens.sh` (paid-user tracking moved to DB tables).
- Changed: `scripts/snapshot_minds_e2e_state.py` sets `LATCHKEY_DISABLE_COUNTING=1` so the snapshot builder does not count toward Latchkey usage; genuine installs still count.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace, reads inbound-SSH keys from a checked-in `.github/tmr-authorized-keys`, drops the removed `--use-snapshot` flag, and passes AWS secrets for the S3 report mirror.
- Changed: Collapsed Modal environments across `just test-offload-acceptance` / `test-offload-release` to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) to stay under the 1500-env-per-workspace cap; `sdk_live` tests are excluded from CI.
- Changed: Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear, no-behavior-change improvement.
- Changed: `scripts/install.sh` invokes `mngr dependencies --install interactive --scope core`, so a missing optional dependency (`ssh` / `rsync` / `unison` / `claude`) no longer trips the installer warning -- only missing core dependencies do.
- Changed: Updated the local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`) to the full current Anthropic Claude lineup with per-token pricing, kept in sync with `apps/modal_litellm/app.py` by a drift test.
- Changed: `.gitignore` now ignores `**/*.local.sh` (mirroring `**/*.local.md`) and broadens the `_tasks/` rule to `**/_tasks/` so the root-level `dev/_tasks/` output folder is ignored too.

### Removed

- Removed: Broken `cleanup-pool-hosts` justfile recipe -- it sourced the long-gone `.minds/<env>/neon.sh` files (secrets moved to Vault) and was redundant with the connector's hourly release-cleanup cron; `destroy-pool-host` is the env / Vault-aware replacement.
- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, ratchet exclusions, `uv.lock` workspace member).
- Removed: `test_no_dependencies_younger_than_two_weeks` from `test_meta_ratchets.py` -- the cooldown is now enforced at lock time via `[tool.uv] exclude-newer`, so the time-relative test is redundant.

### Fixed

- Fixed: Nightly changelog consolidation schedule fired at 8 AM Pacific instead of midnight -- the cron was set to `0 8 * * *` assuming UTC, but it is interpreted in the deploying machine's local timezone. Now uses `0 0 * * *` with an explicit `--timezone America/Los_Angeles`.
- Fixed: `just test-acceptance` marker expression was `-m "no release"` (a pytest syntax error that failed at collection); now `-m "not release"`.
- Fixed: TMR workflows now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee` pipeline, so a failed run is no longer reported as successful when `pipefail` fails to propagate the left-side failure.
- Fixed: Added a `**/tmr-report/` pattern to the root `.gitignore` (the existing `**/tmr_*/` pattern used an underscore and did not match the dash-named directory).

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
