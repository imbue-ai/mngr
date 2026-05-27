# Unabridged Changelog - mngr_tmr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_tmr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
