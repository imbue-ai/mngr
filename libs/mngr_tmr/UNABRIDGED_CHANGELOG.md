# Unabridged Changelog - mngr_tmr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_tmr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Reworked the TMR agent prompts so generated e2e tests converge to a stable size instead of accreting assertions every run:

- The testing-agent (mapper) prompt now anchors test quality to two sources: the claims the test's tutorial block explicitly makes (a hard requirement), and the effect each command or flag implies (a command must do something observable, and a flag must change something versus running without it -- the assertion should fail if the command were a no-op). Every assertion must trace back to one of those two sources; removing an over-fitted assertion that serves neither is a first-class improvement (recorded as `IMPROVE_TEST`) on par with adding one. Leaving an already-converged test unchanged is an explicitly correct outcome. The old "verify as thoroughly as possible" guidance, which pushed tests to grow without bound, is gone.

- Testing agents now flag cross-cutting setup that they cannot fix from a single test (e.g. mocking a claude/codex agent with `sleep`, a missing shared fixture, or absent credentials) with `# FIXME(tmr): ...` comments instead of papering over them with brittle local hacks.

- The integrator (reducer) prompt gained a normalize stage that runs on the integrated suite: it extracts genuinely-duplicated scaffolding into shared helpers -- but only steps that do NOT come from a test's tutorial block, preserving the 1:1 test/tutorial relationship -- and triages the `FIXME(tmr)` blockers, resolving the ones it can verify suite-wide and escalating the rest.

- The integrator outcome schema and the HTML report now carry `normalizations` (suite-wide cleanups applied) and `escalations` (blockers surfaced to the user), so unresolved cross-cutting issues are visible in the report rather than silently dropped.

- The reducer prompt now tells the integrator to verify changes by running the affected e2e tests directly with pytest (scoped to the blast radius they touched) in its own work_dir before publishing. Verified end to end on a trial run: a reducer integrated two mappers' assertion trims and re-ran the affected help tests to confirm them.

## 2026-06-12

## Fix snapshot launch path after provider bootstrap refactor

The AWS-provider shared-layer refactor removed the `is_for_host_creation` parameter from `get_provider_instance`, but `mngr_tmr`'s `--use-snapshot` launch path still passed it, which broke the type check. The snapshot path now calls the new `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` helper before `get_provider_instance`, matching how `mngr create` triggers one-time backend bootstrap (e.g. Modal's per-user environment).

## 2026-06-10

Fixed a stale See-Also reference in the `tmr` command's help metadata. The `pull` reference pointed at a top-level command that was removed when push/pull were restructured into `rsync` and `git push`/`git pull`; it is now replaced with an `rsync` reference. Previously this produced a broken `[mngr help pull](mngr help pull)` markdown link in the generated docs.

Raised the stale coverage floor from 50% to 75% to match the coverage CI already measures (~78%).

## 2026-06-08

- Stays unpublished-on-purpose (already in `UNPUBLISHED_PACKAGES`; the canonical mapreduce recipe, internal tooling). Its stale `imbue-mngr==0.1.6` pin is realigned to the current `0.2.10` so `uv lock` stays solvable. No runtime change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

Marked the `TestRunInfo`, `TestResult`, and `TestMapReduceResult` result models
with `__test__ = False` so pytest no longer attempts to collect them as test
classes (their names start with "Test"). This silences the "cannot collect
test class ... because it has a __init__ constructor" warnings in CI. No
behavior change.

## 2026-05-28

`mngr_tmr` is now a thin recipe on top of the new `mngr_mapreduce` framework. The `mngr tmr` CLI surface is unchanged for users; under the hood, all agent launching / polling / extraction code moved out, and TMR is now expressed as a `TestMapReduceRecipe` (in `imbue.mngr_tmr.recipe`) implementing discovery (pytest collect), prompt building, and the `on_mapper_finalized` / `on_reducer_finalized` hooks (which apply each agent's `branch.bundle` to the local repo). Server-side labels were renamed to `mapreduce_role` / `mapreduce_run_name` and the outputs-archive path was simplified to `plugin/mapreduce/outputs.tar.gz` — agents from older TMR runs are not discoverable by this version (run them down with the prior `mngr` build first).

Integrator now runs on the same `--provider` as the testing agents and reuses any snapshot the testing agents built, so on `--provider modal` (or any remote provider) it spins up just as quickly as the test agents do instead of running locally.

To make that work, the integrator now publishes its results the same way testing agents do — packaging `test_output/` + `branch.bundle` into `outputs.tar.gz` under `$MNGR_AGENT_STATE_DIR/plugin/test-map-reduce/` — so the orchestrator can pull and apply the integrated branch via the same volume-based path it already uses for testing-agent outputs.

The integrator path is now identical across providers (including local): the orchestrator rsyncs the local output directory (every testing agent's extracted outputs) into `<work_dir>/.tmr_inputs/` on the integrator host and then sends the integrator prompt; the prompt's inline bash walks each subdirectory, applies the "should pull" predicate to filter qualifying agents, fetches the qualifying bundles into local branches, and cherry-picks. Achieving this required switching local testing agents from `GIT_WORKTREE` to `GIT_MIRROR` transfer mode — branches now live only in each agent's own clone (rather than auto-appearing in the orchestrator's source repo) and surface in the source repo only via the published bundle. Slightly slower than the worktree mode on local, but the unified code path makes the local provider a meaningful proxy for testing the remote one.

Removed CLI flags (the integrator now follows the testing-agent settings): `--integrator-provider`, `--integrator-type`, `--integrator-template`. Pass `--provider` once for both. `--integrator-timeout` is unchanged.

Also dropped `--use-snapshot`: snapshot building is now automatic whenever the provider supports it (modal today, with no warning when the provider doesn't). `--snapshot <ID>` still works for reusing an existing snapshot.

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

## 2026-05-26

## mngr_tmr

- `mngr tmr --provider modal --use-snapshot` now bootstraps the Modal per-user environment on first run instead of aborting with `ProviderEmptyError`. The pre-snapshot provider lookup passes `is_for_host_creation=True`, matching the create path.
- Several silent-success failure modes now produce a non-zero exit (click's default exit code):
  - `--reintegrate` when `mngr list` fails or the run name matches no agents.
  - Any tmr run where every test agent failed to launch (no successful launches).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- TMR run names are now a single compact timestamp `YYYYMMDDHHMMSS` (e.g. `20260514184215`) used consistently across the output directory (`tmr_<run>/`), the `tmr_run_name` agent label, and the agent / host / branch names of every TMR-spawned entity. Testing agents are `tmr-<run>-<test_name>` (with `-2`, `-3`... appended on sanitization collisions; the random hex id has been removed), branches are `mngr-tmr/<run>/<test_name>`, the snapshotter and integrator are `tmr-<run>-snapshotter` / `tmr-<run>-integrator`, and the host pool is `tmr-<run>-host-<i>`. A new `tmr_role` label (`testing` / `snapshotter` / `integrator`) replaces the previous name-prefix matching for filtering integrator agents during `--reintegrate`.
- The TMR HTML report is now mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html` (us-west-2) on every regeneration when `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are set, and the public URL `http://go/shared/tmr-reports/<run>.html` is printed (and emitted as a structured `report_url` event in JSON/JSONL output).
- Added a `--run-name` flag to `mngr tmr` to override the auto-generated run name.
- Internal cleanup: the `tmr_role` agent label is now derived directly from `AgentKind` (which gained a `SNAPSHOTTER` variant) and stamped centrally inside `_create_tmr_agent`, so a single `kind: AgentKind` argument controls both in-process classification and the on-server label. The S3 mirror of the HTML report is now invoked from the orchestration / cli layers rather than from inside `report.generate_html_report`, restoring the reporter to its previous "writes a file, returns a Path" contract.

## 2026-05-14

`mngr tmr`: testing agents now publish a single `outputs.tar.gz` archive into
their state directory (`$MNGR_AGENT_STATE_DIR/plugin/test-map-reduce/`),
containing the renamed `test_output/` directory and an optional incremental
`branch.bundle`. The orchestrator polls for the archive via the per-agent
volume API (which works even when the host is offline) and reconstructs the
agent's branch from the bundle, removing the previous rsync + git-pull
finalization step. Reintegrate mode uses the same path. SSH provider, which
does not expose a volume, is no longer supported for testing-agent outputs.
The integrator agent is unchanged.

Regenerated CLI docs for `mngr tmr` to reflect current options.

## 2026-05-12

TMR: when running against a remote provider with `--use-snapshot` (or
`--snapshot=<id>`), avoid re-uploading the code repo for every test agent.
The snapshotter agent's work_dir is now pinned to `/code` on its host, and
each test agent created from the resulting snapshot sources from that
on-host `/code` via `git-worktree` -- previously each agent re-pushed the
git history from the laptop.

`mngr tmr` accepts a new repeatable flag `--additional-authorized-host`
that adds SSH public key lines to the `authorized_keys` file installed
on each agent host (test agents, host pool, snapshotter, and
integrator). This lets you SSH directly into any agent host TMR
creates, primarily for live debugging.

## 2026-05-08

Add `.github/workflows/tmr.yml`: a manually-dispatched CI workflow that runs `mngr tmr` against Modal, uploads the HTML report as an artifact, and opens a draft PR for the integrator branch. The provider is hardcoded to `modal`; `test_paths`, `pytest_args`, and `agent_type` are exposed as workflow inputs, with defaults reproducing the local invocation against `libs/mngr/imbue/mngr/e2e/test_basic.py -m release` with `--agent-type yolo`.

The `mngr tmr` CLI also now emits an `integrator_branch` event on its structured stdout stream (in `--output-format jsonl`/`json`), so consumers like the new workflow can pick up the branch name without parsing human-formatted output.

## 2026-05-06

- `mngr tmr` no longer crashes the whole orchestrator when a single agent
  fails its initial-message send (e.g. `SendMessageError` from the tmux
  paste-detection timeout). The launching loops now also catch `AgentError`
  alongside `MngrError` / `HostError`, log a warning, and continue with the
  remaining agents. This applies to test-agent launching (both batched and
  pre-launched modes) and to the integrator launch.
- Fix `mngr tmr` integrator launch (and any local-provider test-agent
  launch), which always failed with `Failed to generate a unique host name
  after 100 attempts`. The local provider has a single fixed host
  ("localhost"), so the new-host path can never find a free name; TMR now
  reuses the existing local host when the target provider is `local`,
  matching what `mngr create` already does.
- `mngr tmr` HTML reports now include rows for tests whose agent failed to
  launch (e.g. `SendMessageError` from a paste-detection timeout). They are
  rendered as errored entries instead of being silently dropped, and carry
  the actual agent name that was used for the failed launch attempt -- so
  the report row matches the host/tmux session if the user kept it for
  debugging.
- `mngr tmr` HTML reports now have a dedicated "Failed" section,
  separate from "Blocked". The two represent different failure modes:
  Blocked means the coding agent reported every change as BLOCKED
  (i.e. it considered the work too complex), while Failed means an
  infrastructure failure prevented the agent from producing a verdict
  (launch failed, agent timed out, agent details missing). Errored
  results that previously fell into "Blocked" now route to "Failed".
