# Unabridged Changelog - mngr_tutor

Full, unedited changelog entries for the `mngr_tutor` project, consolidated nightly from individual files in `libs/mngr_tutor/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-01

urwid 4.0.13 types an alarm callback's user data as optional, because `set_alarm_in`'s `user_data` defaults to None. The lesson runner's check alarm accepts the optional form and checks it; the scheduling site still passes the state.

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

## 2026-06-18

Internal refactor: the tutor's tmux-session-has-clients check now builds the agent's tmux session name via the shared `MngrConfig.agent_session_name` helper instead of hand-rolling the `prefix + name` string, keeping it consistent with mngr's centralized session-name construction. No user-visible behavior change.

## 2026-06-10

Raised the stale coverage floor from 90% to 95% to match the coverage CI already measures (~96%).

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Updated to the repo-wide error-hierarchy consolidation: the check runner's
`except (BaseMngrError, OSError)` now reads `except (MngrError, OSError)` (`BaseMngrError` has
been removed). No behavior change.

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

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
