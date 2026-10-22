# Unabridged Changelog - resource_guards

Full, unedited changelog entries consolidated nightly from individual files in `libs/resource_guards/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-02

- New `register_session_cleanup_callback(callback)` and `run_session_cleanup_callbacks()`: a library can register, alongside its guards (typically from its `resource_guards` entry point), a callable that the shared conftest hooks run once at the end of every pytest session, after the last test's teardown and before the guards are torn down. Meant for sweeping external resources that tests leaked, such as docker containers. Duplicate registrations of one callable are ignored.

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

Gave `test_blocked_resource_appended_to_failing_test` a longer (30s) per-test timeout. It spawns a pytest subprocess via `runpytest_subprocess`, whose startup is slow and variable under offload load and intermittently exceeded the default 10s timeout. No change to `resource_guards` runtime behavior.

Trimmed the README to user-relevant content, cutting the internal guard mechanics and tightening prose for concision.

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

## 2026-05-27

# ty 0.0.39 type fixes

- `fixture_uses_resources`'s `TypeVar` is now bound to a small `_NamedCallable` protocol (a callable that also carries `__name__`), so reading `func.__name__` in the misconfiguration error type-checks under `ty` 0.0.39. The decorator is only ever applied to fixture functions, which satisfy the protocol.
- The pytest hookwrapper generators (`_pytest_fixture_setup`, `_pytest_runtest_makereport`, and their plugin-class wrappers) now annotate their generator send type as `pluggy.Result[...]`, so `outcome.get_result()` / `outcome.excinfo` resolve instead of being treated as attributes of `None`.

No user-facing behavior change.

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

Add `@fixture_uses_resources` to `imbue.resource_guards` for declaring resource use at the fixture level. Module/session-scoped fixtures that opt in run their setup and teardown under their own guard scope, so resource calls inside the fixture are authorized against the fixture's declaration rather than the consuming test's marks. Untouched fixtures keep existing behavior.

Adjust the mark semantics around `@fixture_uses_resources`:

- `@pytest.mark.<resource>` on a test is now satisfied by either direct resource invocation in the test body OR by a `@fixture_uses_resources(<resource>)` fixture in the test's closure.
- The mark is now **required** on every consumer of a tagged fixture, even consumers that don't directly invoke the resource. This makes `pytest -m <resource>` the canonical selector for every test that transitively needs the resource, with no silent escape hatch.
- The block check (calls without the mark) is unchanged.
