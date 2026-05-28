# Unabridged Changelog - imbue_common

Full, unedited changelog entries consolidated nightly from individual files in `libs/imbue_common/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Add a `PREVENT_BARE_TMUX_TARGETS` ratchet rule (and `check_bare_tmux_targets` helper)
that flags `tmux <subcmd> ... -t '<target>'` or `... -t "<target>"` where the quoted
target doesn't begin with `=`. Scans every tracked file type, not just `.py`, so
shell scripts and other non-Python tmux call sites are also covered. Use it from
project ratchet suites (mngr does, via `rc.check_bare_tmux_targets`).

Context: bare-name tmux targets fall back to session prefix matching, which can route
commands meant for a stopped session to a still-running sibling whose name starts with
the same prefix. Routing all `-t` argument construction through the
`TmuxSessionTarget` / `TmuxWindowTarget` classes in `imbue.mngr.hosts.tmux`
(via `.as_shell_arg()`) prepends `=` for exact-match resolution; this ratchet enforces
that convention.

Promote `BINARY_FILE_EXCLUSION` (a tuple of binary-file globs that would otherwise
trip `.read_text()` with `UnicodeDecodeError`) to a public `Final` constant in
`imbue.imbue_common.ratchet_testing.core` so the project ratchets and the repo-wide
meta-ratchets share one canonical list.

## 2026-05-22

- The shared conftest hooks now set `LATCHKEY_DISABLE_COUNTING=1` in `os.environ` once per pytest session. Any subprocess spawned by a test (directly or transitively, e.g. the Latchkey Gateway started by the minds Electron e2e test) inherits the opt-out, so test runs no longer count toward Latchkey's public daily usage counter.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-08

- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
