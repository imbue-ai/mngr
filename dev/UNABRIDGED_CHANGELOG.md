# Unabridged Changelog - dev

Full, unedited changelog entries consolidated nightly from individual files in `dev/changelog/`. Covers repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-28

Added `libs/mngr_mapreduce` to the workspace; root `pyproject.toml` now collects coverage for `imbue.mngr_mapreduce` alongside the other workspace packages.

## 2026-05-27

# Bump `ty` to 0.0.39, plus paramiko/coolname dependency bumps

- Raised the `ty` type checker floor from `0.0.24` to `0.0.39` (root `pyproject.toml`).
- Bumped pinned dependencies in `uv.lock`: `paramiko` 3.5.1 -> 4.0.0 and `coolname` 3.0.0 -> 5.0.0. The paramiko bump also pulls `pyinfra` 3.6.1 -> 3.8.0 and adds `invoke` and `types-paramiko` transitively (pyinfra 3.8.0 depends on `types-paramiko`).
  - Note: paramiko 4.0.0 is the ceiling while we depend on `pyinfra`; pyinfra 3.8.0 constrains `paramiko<5`, so paramiko 5.0.0 is not yet installable.
  - The newly-present `types-paramiko` stubs make ty type-check paramiko usage for the first time; resulting type errors were fixed across the affected projects.
- Behavioral note for contributors: `ty` 0.0.39 no longer honors the bracketed PEP-484 form `# type: ignore[<mypy-code>]`. Only bare `# type: ignore` and `ty`'s own `# ty: ignore[<ty-rule>]` are respected. All bracketed `# type: ignore[...]` comments in the repo were converted to `# ty: ignore[...]` using ty's rule names.
- Documented in `CLAUDE.md` (the "# Ratchets" section) how to tighten a ratchet count after reducing violations: `uv run pytest --inline-snapshot=trim <test_ratchets.py>` (only `=trim` lowers a count that already passes its `<=` check; `=fix`/`=update` do not).
- Tightened recorded ratchet violation counts to their current exact values across all projects via `--inline-snapshot=trim`, locking in previously-unrecorded reductions (test-config only; no source or behavior change).
- Ran `uv lock --upgrade` under a two-week supply-chain cooldown (adopting only releases that have been public for at least two weeks) to bump floating dependencies. Notable bumps within that window: `starlette` 0.50 -> 1.0, `urwid` 3.0 -> 4.0, `pydantic` 2.12 -> 2.13, `cryptography` 46 -> 48, `typer` 0.21 -> 0.25, `uvicorn` 0.40 -> 0.46. The cooldown holds back even-newer releases, e.g. `wsgidav` stays 4.3.3 rather than 4.3.4 (4.3.4 adds a `bcrypt<5` cap and a `passlib` dep), so `bcrypt` stays at 5.0.
  - Bumped the `supertokens-python` floor (see the `remote_service_connector` changelog) so the resolver keeps it at the latest 0.31.3 instead of backtracking to 0.30.3; that also keeps `aiosmtplib` at 5.x for free.
- Added `test_no_dependencies_younger_than_two_weeks` (in `test_meta_ratchets.py`) to enforce the cooldown: it fails if any locked dependency was published within the last two weeks, except deliberately-trusted exemptions (`ty` -- our dev-only type checker, pinned to the latest 0.0.39; `modal` -- explicitly pinned to ==1.4.3). uv's static `[tool.uv] exclude-newer` only accepts a fixed date, so the relative cutoff lives in this (time-relative) test instead; regenerate compliant locks with `uv lock --upgrade --exclude-newer "2 weeks"`. The cooldown does not protect us from a compromise that stays undetected past the window, nor the first project to lock a release -- its only value is the detection delay before we adopt (and, for runtime deps in published wheels, re-propagate) a release.

## 2026-05-26

# Repo-root spec annotation

[`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
got a top-of-file banner noting that the per-agent `MINDS_API_KEY` and
the per-agent reverse SSH tunnel for the Minds API are both gone --
agents now reach the API exclusively through the latchkey gateway's
`minds-api-proxy` extension, with a single installation-wide
`MINDS_API_KEY`. See the changelogs for the `minds` and `mngr_latchkey`
projects for the full design + implementation notes.

- Updated the minds Electron acceptance test spec (``specs/minds-electron-acceptance-test/spec.md``) to reference ``launch_mode=DOCKER`` instead of ``launch_mode=LOCAL``, matching the corresponding minds enum rename. The test code in ``apps/minds`` was already updated; this brings the spec in sync.

- Updated the nightly changelog consolidation prompt (`scripts/changelog_consolidation_prompt.md`) so the concise `CHANGELOG.md` is a notable-only summary: non-notable changes (canonically, changes that only affect tests rather than user-facing behavior) are now omitted from `CHANGELOG.md` entirely instead of being forced into a `Changed` bullet. Such entries are still preserved verbatim in each project's `UNABRIDGED_CHANGELOG.md`.
- Added a `dev`-project exception to that rule: because `dev` tracks repo-level developer tooling (CI, scripts, build config, ratchets, the changelog system) rather than product behavior, `dev` entries are judged by developer/maintainer impact rather than end-user-facing behavior.

# CI guard for stale generated CLI docs

`scripts/make_cli_docs.py` gained a `--check` mode that reports any stale
generated docs (and the exact regen command) and exits non-zero without writing
anything. Its content generation was refactored so a single
`collect_generated_files()` function is the shared source of truth for both
writing the docs and checking them, so the writer and checker cannot drift.

A new `test_cli_docs_are_up_to_date` (in `test_meta_ratchets.py`, alongside the
existing repo-wide ruff check) runs that `--check` mode and fails if the
committed CLI docs or PyPI README are out of date, pointing you at
`uv run python scripts/make_cli_docs.py`. This complements the existing
`test_all_non_hidden_commands_have_generated_docs`, which only checks that a doc
file exists per command, by also verifying the file contents are current.

Workspace + scripts metadata follows the rename of `libs/mngr_gemini` to `libs/mngr_antigravity`: workspace `pyproject.toml` cov target, `test_profiles.toml` mngr-suite test paths, top-level `README.md`, and the package list in `scripts/utils.py`.

- Added `specs/env-settings-overrides/concise.md` documenting the new `MNGR__*` env-var override scheme, the `__extend` operator, and the assign-by-default merge semantics shipped with this PR. See the `mngr` changelog entry for the user-visible behavior.

Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear
improvement and doesn't change behavior (e.g. inline-re-construction folded into a
pre-existing local). Previously the rule only listed specific cases.

## 2026-05-26

## dev

- TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline. The implicit `pipefail` propagation was observed to not catch the left-side failure in this step, letting a failed run be reported as successful.

## 2026-05-22

- New direct dependencies recorded in `uv.lock` to support the minds
  WebDAV file-server mount: `wsgidav` (the WebDAV server itself) and
  `a2wsgi` (the WSGI-to-ASGI adapter that bridges it onto Starlette /
  FastAPI). Both are pulled in via `apps/minds/pyproject.toml`.

- The `TMR` GitHub Actions workflow now runs on a daily cron at 08:00 UTC (00:00 PST; shifts to 01:00 PDT in summer, since GitHub Actions cron has no timezone support). The cron lives in a new `TMR (scheduled)` workflow that gates on a prior periodic PR and then invokes the main `TMR` workflow via `workflow_call`; manual `workflow_dispatch` runs of TMR remain independent of the gate.
- The default `test_paths` workflow input now points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`, so both scheduled and one-click runs exercise the full e2e suite.
- Scheduled-run gate behavior:
  - If a prior scheduled run's PR (label: `tmr-periodic`) is open and 4 days old or younger, today's scheduled run is skipped and a new comment is posted on the open PR explaining the policy. The recurring daily nudge is intentional.
  - If the prior PR is more than 4 days old, the gate posts a closing comment, closes the PR (with `--delete-branch`), proceeds with a fresh run, and after the new PR is opened posts a follow-up "Superseded by #N" comment on the closed PR.
- The auto-opened PR from scheduled runs is labeled `tmr-periodic` (the label is created on demand) and assigned to `qi-imbue` and `joshalbrecht`. Manual-run PRs are unlabeled, unassigned, and therefore invisible to the gate.

## Spec: discovery providers and errors

- Add `specs/discovery-providers-and-errors/concise.md` describing the cross-project change that promotes per-provider state (successfully loaded providers, per-provider discovery errors) to first-class fields on `FullDiscoverySnapshotEvent`, replaces minds' silent auto-disable-on-auth-error machinery with a visible providers panel + explicit Enable/Disable toggle, adds a new `UNKNOWN` value to `AgentLifecycleState` / `HostState` for previously-tracked agents whose provider just failed, and teaches `mngr_notifications` to recognize the indirect `RUNNING -> UNKNOWN -> WAITING` transition. See the per-project changelog entries in `libs/mngr/`, `libs/mngr_forward/`, `libs/mngr_imbue_cloud/`, `libs/mngr_notifications/`, and `apps/minds/` for the actual code changes this spec describes.

## 2026-05-21

- `CLAUDE.local.md` is now copied into agent workdirs by default, so user-specific Claude instructions from the host repo are available inside agents.

Adds a `just minds-test-electron` recipe that wraps the new `test_create_local_docker_workspace_via_electron` Electron acceptance test in `xvfb-run -a`, and wires the existing `test-docker` CI job to install Node, pnpm, xvfb, and the apps/minds pnpm dependencies so the Electron binary is available for the run.

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

Add `specs/minds-env-activate-split/concise.md`: design for splitting
`minds env activate` into a default use-mode (no `MODAL_PROFILE`) and an
opt-in `--deploy` mode. Fixes the spurious Modal-discovery warnings and
Latchkey breakage hit by users who activated `staging` only to *use* the
deployed tier but had no Modal token for the `minds-staging` workspace.

Root-level surface changes for the `mngr_uncapped_claude` plugin: README updated to advertise the new `uncapped-claude` command and link to the new sub-project, and the auto-generated CLI docs gained an entry at `libs/mngr/docs/commands/secondary/uncapped-claude.md` so `mngr ask` and `mngr --help` know about the command.

## 2026-05-20

Restructure the changelog system from a single repo-wide changelog to one set of changelog artifacts per project, owned inside each project's own directory.

- Each project (every `libs/<name>` and `apps/<name>`, plus the synthetic top-level `dev/`) now holds three things at its root: `changelog/` (per-PR entry files), `CHANGELOG.md` (concise summary), and `UNABRIDGED_CHANGELOG.md` (verbatim per-date sections).
- Per-PR entry files now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches), instead of a single `changelog/<branch>.md` at the repo root.
- The consolidator (`scripts/consolidate_changelog.py`) walks each project's `<project_dir>/changelog/` and routes its entries into `<project_dir>/UNABRIDGED_CHANGELOG.md`. The machine-readable output format is now one `SECTION <project> <date>` line per inserted section.
- The `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `<project_dir>/changelog/<branch>.md` for each. Adding the entry file inherently satisfies the requirement for the project that owns it; the consolidation cron's own branch prefix is the only special-cased exemption.
- New `test_every_project_has_changelog_layout` meta-ratchet enforces that every project has `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`, and a `changelog/` directory. Stubs were added for projects without entries yet.
- `scripts/changelog_consolidation_prompt.md` updated to parse `SECTION` lines and summarize each project's section into that project's `CHANGELOG.md` `[Unreleased]`.
- `scripts/release.py` finalizes each bumped package's and each first-time-publish package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section. `apps/<name>/CHANGELOG.md` and `dev/CHANGELOG.md` are not versioned, so their `[Unreleased]` accumulates entries indefinitely.
- New shared `scripts/changelog_projects.py` owns the path-to-project mapping (used by the consolidator, the ratchet, and the release script).
- `test_meta_ratchets._get_all_project_dirs` and `all_known_projects` now both build on a shared `pyproject_projects()` helper in `scripts/changelog_projects.py`, instead of `_get_all_project_dirs` going through `all_known_projects` and filtering out the synthetic `dev` bucket.
- The `test_pr_has_changelog_entry` ratchet's "missing entries" failure message now names the resolved diff base and warns that a misconfigured/stale base can make unrelated `main` files appear as if they changed on this branch, falsely implicating projects the PR didn't touch — in which case the right fix is to refetch the base, not to add placebo entries for projects you didn't actually change.

The existing top-level `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files; see each project's `CHANGELOG.md` for its history.

`scripts/release.py` now refuses to cut a release when there are unconsolidated entries in `changelog/`, since those would otherwise be omitted from the version's release notes. When the gate fires it prints the exact one-liner that triggers the `changelog-consolidation` schedule on demand (the same one that normally runs nightly), so the human can run it, land its PR, and re-run the release. The predicate ("are there pending entries?") lives next to the consolidator's own filter in `scripts/consolidate_changelog.py`, and the plugin-disable args used around `mngr schedule` invocations live in `scripts/trigger_changelog_consolidation.py` and are shared by `scripts/setup_changelog_agent.sh`.

Collapse Modal environments across an offload-acceptance / offload-release
run to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`).
Each fanned-out sandbox in `just test-offload-acceptance` and
`just test-offload-release` used to mint its own Modal environment and
delete it on teardown -- dozens to hundreds per run, driving the
1500-env-per-workspace cap into transient failures. The justfile recipes
now pre-create a single `mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>` env
once, forward its name into every sandbox via `--env`, and `trap`-delete
it at recipe exit.

- The TMR GitHub Actions workflow now defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from the checked-in `.github/tmr-authorized-keys` file (in addition to the existing `additional_authorized_hosts` workflow input). To register your key, run `uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/setup_tmr_ci_debug.py` and append the printed public key to that file via PR; then debug CI-created modal agents locally with `MNGR_HOST_DIR=~/.mngr-tmr-ci uv run mngr list` / `mngr connect`.
- The TMR GitHub Actions workflow passes the AWS secrets through for the S3 report mirror and uses the public URL in the auto-opened PR body, falling back to the existing `tmr-report` artifact when no upload happened.
- The main `TMR` GitHub Actions workflow accepts a corresponding `run_name` workflow_dispatch input, and a new `TMR (reintegrate)` workflow takes that run name back as a required input and runs `mngr tmr --reintegrate <run>` against it (re-running just the integrator phase, opening the same kind of draft PR).
- The two TMR workflows share a new `.github/actions/tmr-setup` composite action for their common setup steps.

## 2026-05-14

CI acceptance test speedups (workflow-side):

1. Grant `contents: write` to the `test-offload` and `test-offload-acceptance` jobs so offload can push its image-cache git notes back to `refs/notes/offload-images`. Previously every run was a cache miss (the `git push` from offload failed with "Permission to imbue-ai/mngr.git denied to github-actions[bot]"), forcing a full `checkpoint_base_prepare` rebuild (~150 s wasted per CI run on acceptance, similar on the regular offload job). Measured saving on cache hit: ~124 s per acceptance run.

2. Lower `max_parallel` from 200 to 50 in `offload-modal-acceptance.toml`. With 200 slots and ~89 tests, offload's LPT scheduler degenerated to one-test-per-batch, so every batch paid full pytest cold-start, Modal sandbox creation, and an orchestrator-side `uv run` cold-start per download. Lowering to 50 lets LPT pack ~2-4 tests per batch (longest single tests still alone via load-balancing). Combined measured saving: ~62% acceptance wall-clock reduction.

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the `.github/workflows/{ci,tmr}.yml` install steps.

Removed the unused `libs/flexmux/` project and all references to it (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions in `test_meta_ratchets.py` and `scripts/sync_common_ratchets.py`, and the `uv.lock` workspace member).

## 2026-05-12

- The changelog consolidator now groups entries by the date their PR landed on `main` (committer date of the introducing commit on the first-parent line, in America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md` (newest first), instead of bucketing everything under the consolidator's run-time UTC date.
- The abridged `CHANGELOG.md` is now version-organized instead of date-organized: a `## [Unreleased]` placeholder sits at the top of the file, the nightly consolidation cron appends categorized bullets (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`) under `### <Category>` subheadings in that section, and `scripts/release.py` renames `## [Unreleased]` to `## [vX.Y.Z] - YYYY-MM-DD` and inserts a fresh empty `[Unreleased]` above it as part of the release commit. Each cron-generated bullet is in the form `- <Category>: <description>`, and the cron does one refinement pass over `[Unreleased]` after drafting to tighten/dedupe before committing.
- Enabled auto-merge on the consolidation cron: each fire now runs `git fetch && checkout main && merge origin/main` before forking the per-run branch, so the eventual PR's diff against `main` is always just the consolidation commit -- no script-snapshot drift even if the cron is redeployed less often than `main` moves.

The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts two new optional `workflow_dispatch` inputs:

- `mngr_user_id`: exported into the orchestrator's process env so the
  `mngr tmr` run attributes the modal agents it creates to that user,
  with the goal of letting them be observed from the user's local
  `mngr list`.
- `additional_authorized_hosts`: one SSH public key per line; each
  non-empty line is forwarded to `mngr tmr` as a separate
  `--additional-authorized-host` argument.

## 2026-05-08

- Fixed the changelog consolidation cron's commit author email: was `dev@imbue.com`, now `bot@imbue.com`, matching the verified email on the bot GitHub account whose token the cron uses to push and open PRs. Without this, GitHub couldn't attribute consolidation commits to the bot user.

- `scripts/setup_changelog_agent.sh` now redeploys when re-run: removes any existing `changelog-consolidation` schedule before recreating, so the deployed schedule always reflects the current source. Drops the `CHANGELOG_REPLACE=1` gate that previously errored on an existing schedule.
- Header docstring now lists the required `GH_TOKEN` (token for `bot@imbue.com`) and `ANTHROPIC_API_KEY` env vars, and includes the on-demand trigger one-liner.

- Removed an unused `# type: ignore[misc]` in `ssh_tunnel_test.py` so the type-error ratchet stops failing on it.

## 2026-05-06

Upgrade offload from 0.8.1 to 0.9.0 and enable history-based test scheduling.
Offload now records per-test durations and uses them to balance sandbox load times,
reducing wall-clock time for the test suite.

Upgrade offload from 0.9.0 to 0.9.2 in CI. Picks up a fix for thin-diff application. Adds the offload binary to the sandbox image (via a multi-stage build) so 0.9.2's `offload apply-diff` step works without falling back to a full rebuild, and propagates `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` through to sandboxes so branch-aware tests like the changelog-entry ratchet identify the PR branch correctly.

## 2026-05-05

Every workspace package's wheel build now excludes test files uniformly via the same canonical line:

```
[tool.hatch.build.targets.wheel]
exclude = ["*_test.py", "test_*.py", "**/conftest.py", "**/testing.py"]
```

Previously, several packages were missing some or all of these patterns and hatchling was shipping `_test.py`, `conftest.py`, and `testing.py` files into published wheels. Notably `libs/mngr` was leaking three test helpers (`cli/testing.py`, `api/testing.py`, `providers/docker/testing.py`) because its existing pattern only covered `**/utils/testing.py`.

A new meta ratchet (`test_every_project_excludes_tests_from_wheel`) enforces the four-pattern rule on every project so this cannot regress.

## 2026-05-02

- Added a changelog system for tracking changes across PRs
  - Per-PR changelog entry files in `changelog/` directory, enforced by CI via meta ratchet test
  - Nightly automated consolidation of changelog entries into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (concise AI-generated summary)
  - Idempotent setup script for the consolidation agent (`scripts/setup_changelog_agent.sh`)
