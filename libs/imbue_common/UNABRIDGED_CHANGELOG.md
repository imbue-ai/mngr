# Unabridged Changelog - imbue_common

Full, unedited changelog entries consolidated nightly from individual files in `libs/imbue_common/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-24

Updated the `PREVENT_HARDCODED_CLAUDE_DIR` ratchet's guidance text to reference
the renamed `find_user_config_in_isolated_mode()` accessor (was
`find_user_claude_config()`). No behavior change.

## 2026-06-19

Clarified the README's one-line description of the library's purpose.

## 2026-06-11

Fixed a bug in the `PREVENT_BUILTIN_EXCEPTION_RAISES` ratchet regex: a trailing `\b` after the opening paren meant it only matched raises whose first argument started with a word character (e.g. `raise OSError(msg)`), missing the common `raise ValueError("literal")` and `raise OSError()` forms. The ratchet now also excludes test files (consistent with tests legitimately raising built-in exceptions to simulate error conditions). Replaced the direct `ValueError` raises in the constrained `primitives` types and the `RegexPattern` validator with dedicated `InvalidPrimitiveValueError` / `InvalidRegexPatternError` exception types.

## 2026-06-10

Raised the stale coverage floor from 88% to 90% to match the coverage CI already measures (~95%), and removed the now-obsolete comment about per-package offload coverage drift (the offload bug that caused that drift has since been fixed, so coverage is deterministic).

## 2026-06-04

Ratchet file scans no longer crash on a tracked symlink that resolves to a directory. The file walker (`_get_all_files_with_extension`) now filters on `is_file()` instead of `exists()`, so a symlink-to-directory (which git lists as a blob but cannot be read as a file) is skipped instead of raising `FileReadError`.

- Refresh the stale test-type docstring in `conftest_hooks.py` that described acceptance tests as running "on all branches except release" and release tests as running "only on release". There is no `release` branch; acceptance tests run on every PR and release tests run via the dedicated Release Tests workflow (manual dispatch and `v*` tag pushes) and TMR. No behavior change.

Added a new common ratchet to the `ratchet_testing` framework: `check_per_file_host_upload` (AST-based `find_per_file_host_uploads_in_loops`) flags `write_file`/`write_text_file`/`put_file` calls inside `for`/`while` loops, steering bulk transfers toward a single rsync (`host.copy_directory`). Recurring per-file-over-SSH uploads have repeatedly caused upload timeouts and 'connection reset / SSH protocol banner' failures (see github issue 1825).

## 2026-06-02

A logging test that imported `BaseMngrError` from `imbue.mngr` (now removed) no longer reaches
into the `mngr` package: it uses a local test-only exception instead. No runtime behavior change.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

Also removed the now-unused `check_no_ruff_errors` helper from
`imbue/imbue_common/ratchet_testing/ratchets.py`: its only callers were the
deleted per-project `test_no_ruff_errors` tests, and the repo-wide ruff test
runs its own `ruff check` / `ruff format --check` invocations rather than using
the helper. (`check_no_type_errors` is kept, since the repo-wide type test uses it.)

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 suppression syntax

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39 (which no longer honors the mypy-style bracketed form). Affected: the `field_ref` proxy returns in `frozen_model`/`mutable_model`, the `entry_points` cache monkeypatch in `conftest_hooks`, and an event-level assignment in the event-envelope test.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

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
