# Unabridged Changelog - mngr_wait

Full, unedited changelog entries for the `mngr_wait` project, consolidated nightly from individual files in `libs/mngr_wait/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
