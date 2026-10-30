# Unabridged Changelog - mngr_wait

Full, unedited changelog entries for the `mngr_wait` project, consolidated nightly from individual files in `libs/mngr_wait/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-08-26

`mngr wait` now documents a fourth exit code. A TARGET that is an agent or host *id* matching no known target exits 8 (`EXIT_CODE_TARGET_NOT_FOUND`) rather than the generic 1, because the lookups it resolves through gained that code on this branch. A TARGET given as a *name* that matches nothing still exits 1, since a name that matches nothing is as likely a typo as a target that is gone.

## 2026-08-18

# Follow mngr's discovery return type

Updated for mngr's `discover_hosts_and_agents`, which now returns a `DiscoveryOutcome` (hosts and agents, the providers that answered, and the providers that could not be reached) rather than a two-tuple. No behavior change here: this reads the same discovered agents off the outcome.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-24

`mngr wait --host` no longer treats `UNAUTHENTICATED` as a terminal host state. It now follows `UNKNOWN` semantics (non-terminal): when a host's access credential is rejected, or the host otherwise cannot be observed, a wait keeps polling rather than settling on that non-evidence. Terminal host states are now just `STOPPED`, `PAUSED`, `CRASHED`, `FAILED`, and `DESTROYED`.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-12

Internal: routed the test-helper agent state-dir path construction through the shared `get_agent_state_dir_path` helper (now in `imbue.mngr.hosts.common`). No behavior change.

## 2026-06-10

Strengthened the mngr-wait test suite so it catches real regressions instead of re-deriving definitions:

- Added an end-to-end integration test (`test_cli.py`) for the `mngr wait` command. It now covers the documented exit-code contract (0 on match, 2 on timeout, non-zero on a bad/unknown target), the already-matched fast path, the positional-plus-`--state` argument union, default-state fallback, invalid-state rejection, and reading the target from stdin. Previously the `wait` command itself was never invoked in tests, so a broken exit code or argument-combining bug would have shipped silently.
- Added direct tests for `poll_target_state`, including its real `HostConnectionError` fallback to the offline-host state (STOPPED/CRASHED) and the agent-target STOPPED behavior. The previous test named for this scenario only fed a canned state sequence and never exercised the fallback; it has been renamed and reworded to reflect what it actually verifies.
- Removed three tautological enum-membership tests of the terminal/valid state collections. (They were initially replaced with exact-set snapshots, but those were just change-detectors mirroring the definitions; the meaningful property -- that the actively-running state is not treated as terminal -- is covered by the retained `..._does_not_include_running` tests.)
- Added `check_state_match` branch coverage for agent targets in states `RUNNING_UNKNOWN_AGENT_TYPE` and `REPLACED`, for a host-only transient state (`BUILDING`) on an agent target, and for the "host RUNNING is ignored when watching an agent" rule when no agent state is present.
- Added tests that `validate_state_strings` deduplicates case-insensitively and that its error message names the offending state and lists the valid states in sorted order.
- Verified the `wait` command is discovered through the plugin manager's `register_cli_commands` hook (not just returned by the hookimpl in isolation), guarding against entry-point/registration regressions.
- Raised the coverage floor from 75% to 90% (CI measures ~92%) and removed the stale comment that kept it low for a now-fixed coverage-nondeterminism offload bug.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON output call site to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
