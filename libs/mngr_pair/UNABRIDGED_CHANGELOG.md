# Unabridged Changelog - mngr_pair

Full, unedited changelog entries for the `mngr_pair` project, consolidated nightly from individual files in `libs/mngr_pair/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Lightly tightened the README for concision.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-29

Mark ``test_unison_syncer_syncs_symlinks`` flaky.

The test's ``wait_for`` only waits for the symlink to land in the target
directory, but the very next assertion checks that the symlink's referent file
``real_file.txt`` also exists. Unison gives no ordering guarantee between two
unrelated files in a single sync sweep, so the symlink can appear before its
target file does. The proper fix is to widen the ``wait_for`` predicate; left
for a follow-up.

## 2026-05-28

### Migrate to the new thin `git_pull`/`git_push` wrappers

`mngr_pair` now composes the thin `git_pull`/`git_push` wrappers (from
`imbue.mngr.api.sync`) with its own stash guard and target-branch checkout
dance. Externally observable behavior of `sync_git_state` (stash mode handling,
target-branch handling) is unchanged.

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
