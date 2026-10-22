# Unabridged Changelog - mngr_schedule

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_schedule/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

Scheduled triggers no longer fail verification because of a provider the container cannot reach.

A Modal container has no docker daemon, so a docker provider there is unreachable for the container's whole life. `mngr list` inside it completes the listing but exits with the provider-inaccessible code, which the verification poll could not tell apart from a real failure, so it timed out while the agent it was watching ran fine. The container's mngr no longer installs the docker plugin at all, so there is no docker backend to register, probe, or fail on.

The `test_schedule_add` release tests also asked their triggers to create an agent of type `echo`, which is not a registered type, so every triggered `mngr create` failed to parse its arguments. They now ask for a `command` agent running `echo`.

`mngr schedule add --include-project-settings` sent the deployer's project-local settings to the wrong path whenever `MNGR_ROOT_NAME` was not the default. A project's config dir is `<project root>/.<root name>`, but the staging hook looked under `.mngr` regardless, so with a non-default root name it staged nothing and those overrides never reached the container. It now asks mngr core for the config file's name instead of rebuilding the path, and the container reads them at the path its own propagated `MNGR_ROOT_NAME` implies.

## 2026-10-08

The recorded `Args:`-in-docstrings ratchet count drops from 1 to 0: its one match was the test class `TestAutoFixCreateArgs`, which the tightened rule no longer counts.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-30

Removed the 6 comment lines that had accumulated inside `libs/mngr_schedule/imbue/mngr_schedule/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-23

Tightened the direct-subprocess ratchet count from 7 to 6 now that the shared rule no longer counts `subprocess.Popen[...]` type annotations as spawns. (Affects test infrastructure only.)

## 2026-09-22

mngr_schedule: stop the `schedule run --provider local` CLI test from creating a real agent.

`test_schedule_run_local_deployed_trigger` deployed a trigger and then ran it for real. A deployed trigger's `run.sh` ends in `exec uv run mngr create --message hello`, so on CI the test started an actual agent creation, which has no bound on how long it takes. Sometimes it outlived the test's 30s timeout, and the `uv` and `mngr` processes it had started survived to the end of the pytest session, where the leak scan found them and failed the run against an unrelated test in another package. The test could not catch a regression either way: its only assertion, `assert isinstance(result.exit_code, int)`, holds for every possible outcome.

The test now puts a stub `uv` first on PATH before deploying, so the generated script execs the stub and exits at once. It asserts that the stub's distinctive exit code reaches the caller, which happens only if the creation record was found, the generated `run.sh` ran, and the CLI passed the script's status through. The `@pytest.mark.timeout(30)` that had been papering over the slow run is gone.

Test-only change; `mngr schedule run` behaves exactly as before.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-16

- `mngr schedule add --provider modal` creates the per-user Modal environment when it does not exist yet, the way `mngr create @.modal` does, instead of failing with "Provider 'modal' has no state yet" on a fresh install (the Modal provider stopped auto-creating its environment on read-only commands, which also caught the schedule deploy).

## 2026-09-10

mngr_schedule: follow the Modal CLI JSON parser to its new home.

`parse_modal_app_listings` moved from `imbue.mngr.utils.modal_cli` to `imbue.mngr_modal.modal_cli`, and `ModalCliOutputError` from `imbue.mngr.errors` to `imbue.mngr_modal.errors`. `remove_modal_schedule` and the `cleanup_modal_app` test helper import from the new locations. No behaviour change.

## 2026-09-09

mngr_schedule: fix two Modal app lookups that could never find an app.

`remove_modal_schedule` (in `implementations/modal/deploy.py`, production code) and the `cleanup_modal_app` test helper both matched `modal app list --json` rows on `"Description"` and read the id from `"App ID"`. The Modal CLI emits `description` and `app_id`, so the match never succeeded: `mngr schedule remove` logged "Modal app not found in environment" and left the app running, and the test helper leaked its app.

Both now parse through mngr's `utils/modal_cli.py`, which raises `ModalCliOutputError` if the payload stops carrying the keys we read.

## 2026-08-17

`get_mngr_dockerfile_path` now resolves the mngr Dockerfile from the `imbue.mngr` package location (via `importlib.resources`) for every install mode, instead of navigating from the enclosing git root in EDITABLE/SKIP mode.

The Dockerfile is a packaged resource at `imbue/mngr/resources/Dockerfile`, so the package-derived path is identical in a standalone checkout and also correct when the monorepo is vendored inside another git repository (where the enclosing git root is not the mngr checkout).

Fix scheduled deployments silently dropping the deployer's `config.toml` and top-level profile files when the mngr host directory lives outside `$HOME`. `get_files_for_deploy` now derives staged destinations from the mngr root-name convention (`~/.{root_name}/...`) rather than the local file's position under `$HOME`, so these files stage correctly regardless of where the host dir physically lives (previously the collection raised and all deploy files were skipped).

(GitHub issue #392)

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-26

Added scope docstrings to this package's release tests so the TMR (test
map-reduce) harness can anchor each test's intended scope on its docstring
rather than on a tutorial block. Docstring-only; no test logic changed.

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Bumped the timeout for `test_schedule_run_local_deployed_trigger` to 30s to reduce flakiness. No user-facing changes.

## Test reliability

- Marked `test_schedule_run_local_deployed_trigger` with `@pytest.mark.flaky` so offload retries it. It passes locally (~5s) but occasionally exceeds the 10s pytest-timeout under offload load. This is unrelated to the Azure-provider work on this branch -- it just surfaced on this PR's CI run.

## 2026-06-15

Marked the `test_schedule_run_local_deployed_trigger` integration test `@pytest.mark.flaky` with a longer per-test timeout. The test executes a deployed trigger's `run.sh`, which shells out to `mngr`; that subprocess startup is slow and variable under CI load and intermittently exceeded the default 10s pytest-timeout. No change to `mngr_schedule` runtime behavior.

## 2026-06-14

- Fixed: `mngr schedule remove` (and the redeploy path that calls it) now passes `--yes` when stopping a schedule's Modal app, so it no longer aborts with "no interactive terminal detected" under newer Modal CLIs when run non-interactively (e.g. from a deploy script).

## 2026-06-12

Added a `--timezone` option to `mngr schedule add` that pins the IANA timezone
in which the `--schedule` cron expression is interpreted (e.g.
`--timezone America/Los_Angeles`).

Previously the cron was always interpreted in the deploying machine's local
timezone, so the same schedule could fire at different wall-clock times
depending on where it was deployed from. Pinning `--timezone` makes the fire
time deterministic. The value is validated against the IANA timezone database
at deploy time. The option is only supported for the modal provider; passing it
with `--provider local` is an error.

## 2026-06-11

Replaced direct ValueError raises in modal deploy upload-spec parsing with a dedicated UploadSpecError exception type.

## 2026-06-08

- Now auto-discovered as a publishable package by the release tooling. This also fixes a latent bug: `imbue-mngr-schedule` is already listed in the mngr install catalog, so the wizard offered it even though it had never been published (a user picking it hit a PyPI 404). It will be offered for first publication on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `imbue-mngr-modal`) are now pinned with `==` to their current workspace versions. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `imbue-mngr-modal`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON/JSONL output call sites to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

## 2026-06-01

Fixed the PyPI package names in the PACKAGE-mode Modal deploy Dockerfile generator: it now installs `imbue-mngr` and `imbue-mngr-schedule` (the published distribution names) instead of `mngr` / `mngr-schedule`, which do not resolve on PyPI. (Note: `imbue-mngr-schedule` is not yet published, so PACKAGE-mode schedule deploys still require publishing it.)

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.

## 2026-05-14

`mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent that lives inside the cron-runner's local provider (i.e. inside the ephemeral Modal container). Previously the deploy machine could not reach into the container to observe or destroy the agent, so verify failed for that configuration. Verification now runs inside the container itself and reports the result back to the deploy machine over a structured sentinel line.
