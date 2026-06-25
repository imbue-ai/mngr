# Unabridged Changelog - mngr_schedule

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_schedule/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
