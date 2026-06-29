# Unabridged Changelog - mngr_mapreduce

This file contains the full, verbatim per-PR entries for the `mngr_mapreduce` library. For the curated summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-18

Reduced flakiness in `test_run_mngr_raw_returns_finished_process`. Its `_run_mngr_raw` subprocess budget was 10s, which under heavy parallel CI load left no headroom for a cold `mngr` start and intermittently raised `CliError` before the command actually finished. The budget is now 25s, under the test's existing 30s `pytest.mark.timeout` backstop. Test-only change.

## 2026-06-17

The reducer-prompt send now routes through `require_interactive_agent(...)` before calling `send_message`, since `send_message` is no longer a universal `AgentInterface` method (it moved onto interactive agents). The reducer is a Claude agent (interactive), so behavior is unchanged; the guard just makes the interactive requirement explicit and type-safe.

## 2026-06-16

## Tests

- Marked `test_run_mngr_raw_returns_finished_process` as `@pytest.mark.flaky`: its `mngr config list` subprocess has a hard 10s budget, and a cold `mngr` start under heavy offload parallelism can occasionally exceed it. Offload now retries it automatically.

`stop_agent_on_host` now also tolerates the `CleanupFailedGroup` that `Host.stop_agents`
raises when cleanup leaves a resource behind, so a best-effort stop in a `finally` logs and
continues instead of masking the real result.

`test_run_mngr_raw_returns_finished_process` no longer races the global 10s pytest timeout
against its own 10s subprocess budget: the test function now gets a 30s timeout so a slow
cold `mngr` start under load no longer flakes it.

## 2026-06-08

- Marked unpublished-on-purpose in `UNPUBLISHED_PACKAGES` (it is an internal map-reduce framework library with no CLI of its own, consumed only by recipes like `mngr_tmr`), so the release tooling will not offer it for publication. Its stale `imbue-mngr==0.1.6` pin is realigned to the current `0.2.10` so `uv lock` stays solvable. No runtime change.

## 2026-06-04

Updated the `get_local_host` import to its new canonical home in `imbue.mngr.api.providers` (it previously lived in `imbue.mngr.cli.headless_runner`). No behavior change.

## 2026-06-04

- `sanitize_for_agent_name` now also strips trailing hyphens after the 40-char truncation, not just before it. Without this, a task slug whose 40th character was a hyphen (e.g. `test_create_modal_idle_mode_ssh_timeout_300`) produced `test-create-modal-idle-mode-ssh-timeout-`, which `AgentName` rejects as "alphanumeric with dashes/underscores allowed in the middle". TMR runs against the e2e test corpus hit this for the first time once `test_create_modal_idle_mode_ssh_timeout_300` landed.

## 2026-06-03

Restored the project's `changelog/` directory by re-adding the `.gitkeep` that every project keeps to hold per-PR entries. The directory had vanished on `main` after a changelog-consolidation run emptied it (it never had a `.gitkeep`), which broke the repo-wide `test_every_project_has_changelog_layout` check for everyone who merged `main`. No functional or user-facing change to `mngr_mapreduce`.

- The reducer agent now benefits from the same snapshot-based code reuse optimization as mappers: when the run uses a snapshot, the reducer's host is pre-created so the agent's source is git-worktreed off the snapshot's `/code` instead of re-uploaded from the laptop. Previously only mappers (which shared a pre-created host pool) hit this fast path; the reducer always re-uploaded the source.

## 2026-06-02

Simplified exception handlers now that `AgentError` is a `MngrError` subclass: the redundant
`AgentError` entry in the `except (MngrError, AgentError, ...)` guards in launching and the CLI
has been removed. No behavior change -- agent errors are still caught and handled the same way.

Simplified exception handlers now that `HostError` is a `MngrError` subclass: the redundant
`HostError` entry in the `except (MngrError, HostError, ...)` guards in launching and the CLI
has been removed. `AgentError` (still a `BaseMngrError`, not a `MngrError`) is retained. No
behavior change.

## 2026-06-01

Restored the `changelog/` directory by adding a `.gitkeep` placeholder. The directory had vanished from git after a consolidation run deleted its last entry file (git does not track empty directories), which broke the `test_every_project_has_changelog_layout` meta-ratchet. The `.gitkeep` keeps the directory tracked even when it holds no pending entries, matching every other project. No production code change.

## 2026-05-29

Move post-finalize ``stop_agent_on_host`` calls off the polling loop's main thread.

When a mapper publishes outputs and the underlying remote sandbox (e.g. a Modal
sandbox) has already been torn down, the SSH ``stop_agents`` call blocks on the
kernel's TCP retransmit timeout -- observed at ~16 minutes per call. The
previous synchronous code path serialized the polling loop on those waits,
which left ~50 of 80 mappers unfinalized when a TMR run hit the 4h GHA cap.

Changes:
- Introduce ``_BackgroundStopper`` in ``orchestration.py``: a small
  context-manager helper that spawns an ``ObservableThread`` per stop and
  context-exits with a bounded 60s drain. Anything that escapes
  ``stop_agent_on_host``'s own ``(MngrError, HostError)`` catch is still
  logged via ObservableThread's error logger, but suppressed on join so a
  rogue stop can't crash the drain.
- ``launch_and_poll_mappers`` and ``wait_for_reducer`` now hold a stopper
  for the lifetime of their polling loop and route the post-finalize and
  per-agent-timeout stop calls through it instead of calling
  ``stop_agent_on_host`` synchronously. The mapper-finalize helper takes
  the stopper as a new parameter.

No changes to mngr core; this is a pure orchestrator-side workaround.

## 2026-05-28

Removed the per-project `test_no_type_errors` and `test_no_ruff_errors` ratchet tests from `mngr_mapreduce`. These checks now run repo-wide from the root `test_meta_ratchets.py`, and the per-project copy imported `check_no_ruff_errors` (which was never centralized into `ratchet_testing.ratchets`), producing a ty `unresolved-import` error.

Introduces `mngr_mapreduce`, a Python framework that generalizes the test-fanout pattern previously baked into `mngr_tmr` into a reusable map-reduce engine. Recipes subclass `MapReduceRecipe` to plug in discovery (`discover`), per-task prompts (`build_mapper_prompt`), the reducer prompt (`build_reducer_prompt`), and post-extraction hooks (`on_mapper_finalized`, `on_reducer_finalized`). The framework handles agent launching (with snapshot/host-pool support), polling, outputs-archive extraction, and report rendering/upload; the framework is content-agnostic, treating each agent's `outputs.tar.gz` as opaque and handing the extracted directory to the recipe for interpretation.

### Migrate to the new `imbue.mngr.api.rsync` interface

`mngr_mapreduce`'s reducer-launch path now calls `rsync_to_remote` (from
`imbue.mngr.api.rsync`) instead of the removed `push_files` wrapper.
``extra_args`` replaces the dropped ``is_dry_run``/``is_delete`` parameters,
and the source path is passed with an explicit trailing ``/`` since mngr no
longer mangles slashes on the caller's behalf. Behavior is unchanged.

## [Unreleased]
