# Unabridged Changelog - mngr_notifications

Full, unedited changelog entries for the `mngr_notifications` project, consolidated nightly from individual files in `libs/mngr_notifications/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-23

Updated a watcher test's fake process to pass a `ShutdownEvent`, which `RunningProcess` now requires in place of a plain `threading.Event`. (Affects test infrastructure only.)

## 2026-09-21

`mngr notify` now asks mngr whether an observer is running instead of working it out for
itself.

mngr gained `is_observe_writer_running`, a supported probe for "does some process hold the
observe lock". `mngr notify` had its own version, which answered the same question by
taking the lock and immediately dropping it again. The shared one is the better probe: it
opens the lock file read-only and never creates it, so probing no longer writes a lock
file into a host dir that has never had an observer, and an observer whose lock file
another user owns is still seen.

A probe that cannot answer at all (a permission problem, say) now raises rather than
quietly reporting "no observer". `mngr notify` says so and starts one anyway, which is the
failure that announces itself: if an observer really was running, the child exits
immediately on the lock and the watcher reports it, whereas assuming one is running would
leave `mngr notify` silently waiting on events nobody writes.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-08-16

The notifications watcher's RUNNING-before-UNKNOWN tracking is now keyed by the agent instance (`<agent_id>@<host_id>`) instead of the bare agent id, so two same-id agents on different hosts (agent ids are unique per host, not globally) no longer share the notification-suppression bit. Old event lines without host details fall back to the previous bare-id behavior.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

Removed the now-vestigial `NotificationsPluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

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

## 2026-05-22

## Recognize the indirect "now waiting" transition

- The watcher now fires its "agent is waiting for input" notification for the new `RUNNING -> UNKNOWN -> WAITING` sequence in addition to the existing direct `RUNNING -> WAITING` transition. `AgentObserver` writes an `UNKNOWN` agent state whenever the agent's provider could not be reached during the most recent discovery attempt; an UNKNOWN agent that subsequently lands in `WAITING` (because the provider recovered or the user reached it via another path) now triggers the same desktop notification as a direct transition would.
- Internally, the watcher carries a per-agent "was RUNNING before going UNKNOWN" bit so it can bridge the indirect transition; the bit is cleared on any other transition out of UNKNOWN (notably `UNKNOWN -> RUNNING`).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
