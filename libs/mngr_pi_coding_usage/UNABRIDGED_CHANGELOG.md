# Unabridged Changelog - mngr_pi_coding_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_pi_coding_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Corrected the package version back to 0.1.0 (it had been bumped to 0.1.1, but that version was never actually released).

## 2026-06-16

New package `imbue-mngr-pi-coding-usage`: cost/usage tracking for pi agents in `mngr usage`. pi reports per-message cost client-side, so it's REPORTED (not estimated) and aggregated session-incrementally. Because pi loads a single explicit extension, the per-message writer lives in mngr_pi_coding's lifecycle extension; this package owns the reader (an `aggregate_usage_source` hookimpl claiming the `pi-coding` source) and provisions a `pi_emit_usage` gate marker so the extension only emits usage events when this package (their reader) is installed.
