# Unabridged Changelog - imbue_common

Full, unedited changelog entries consolidated nightly from individual files in `libs/imbue_common/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-22

- The shared conftest hooks now set `LATCHKEY_DISABLE_COUNTING=1` in `os.environ` once per pytest session. Any subprocess spawned by a test (directly or transitively, e.g. the Latchkey Gateway started by the minds Electron e2e test) inherits the opt-out, so test runs no longer count toward Latchkey's public daily usage counter.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-08

- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
