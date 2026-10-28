# Unabridged Changelog - mngr_codex_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-27

Lowered the trailing-comment ratchet count now that the shared rule no longer counts bare `# noqa` directives.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-22

The Codex cost-estimate test now prices its events as `openai/gpt-6-astra`, since `openai/gpt-5.2-codex` is no longer in the usage pricing table.

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

## 2026-06-17

Bumped the trailing-comment ratchet snapshot to account for the converter's pre-existing `# noqa` lint suppression (an immovable trailing comment), surfaced by CI on the agent-mixins branch.

## 2026-06-16

New package `imbue-mngr-codex-usage`: cost/usage tracking for Codex agents in `mngr usage`. Codex reports cumulative token usage (no dollar cost), so cost is estimated from the pricing table and aggregated session-cumulatively. The writer (`codex_usage.sh`) reads codex's rollout stream and emits one `cost_snapshot` per `token_count` item; mngr_codex's background-tasks supervisor launches it when present. It also maps codex's 5h/7d rate-limit windows (subscription mode), so Codex subscription agents get Claude-style windows.

The writer tracks a byte-offset cursor (persisted under `plugin/codex/.usage_cursor`) so each poll processes only the new tail of the rollout (O(new bytes) per tick), important for long Codex sessions. Re-emission after a crash is harmless because the session-cumulative reader keeps the freshest reading per session.

The writer delegates event emission to a standalone `codex_usage_emit.py` (installed alongside `codex_usage.sh` and invoked by it), keeping that logic type-checked, linted, and unit-tested directly. Malformed rollout lines and unreadable cursor state are logged at warning level.
