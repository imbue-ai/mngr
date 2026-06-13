# Unabridged Changelog - mngr_file

Full, unedited changelog entries for the `mngr_file` project, consolidated nightly from individual files in `libs/mngr_file/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-12

Internal: import `get_agent_state_dir_path` from its new canonical location `imbue.mngr.hosts.common` (relocated there from `imbue.mngr.hosts.host` to avoid circular-import issues). No behavior change.

## 2026-06-10

Improved the quality of the mngr-file test suite (test-only changes, no behavior change):

- Added a real end-to-end test that drives `mngr file put` and `mngr file get` through the CLI and asserts the content round-trips, replacing a test that bypassed the command code and only exercised the underlying host interface.
- Strengthened the localhost listing test to create a known file and directory and assert they appear with the correct type and size, instead of merely asserting the listing was non-empty.
- Replaced tautological `FileEntry` constructor assertions with tests that verify immutability and that optional fields default to `None` when omitted, and removed the tautological enum-value assertions entirely.
- Tightened the CLI argument-validation tests to assert a usage error (exit code 2 with a "Missing argument" message) rather than just a non-zero exit code.
- Parametrized the three near-identical `_is_volume_accessible_path` tests into one.

Fixed stale See-Also references in the `file` command's help metadata. The `push` and `pull` references pointed at top-level commands that were removed when push/pull were restructured into `rsync` and `git push`/`git pull`; they are now replaced with a single `rsync` reference. Previously these produced broken `[mngr help push](mngr help push)` markdown links in the generated docs.

Removed a stale `[tool.coverage.report]` comment that attributed coverage oscillation to test nondeterminism. The underlying offload bug that caused the oscillation has since been fixed, so coverage is deterministic; the floor is left unchanged at 75% (CI measures ~79%).

## 2026-06-09

`mngr file get`, `list`, and `put` now operate through the unified host file interfaces
instead of branching internally between an online host and a separately-fetched volume.

- Target resolution returns a single readable host (an online host, or a volume-backed stopped
  host) addressed by absolute paths under the host's `host_dir`; the previous
  `(online_host, volume)` pair and the per-command "volume path" computation are gone.
- `get` reads via `host.read_file`; `list` lists via `host.list_directory`; `put` writes via the
  host's write interface (`HostFileWriteInterface`) for both online and stopped hosts.
- `mngr file list`'s duplicate cross-platform listing script was removed in favor of the shared
  `host.list_directory`. The shared listing now carries the full file type and a permissions
  string when the source can report them: a host (online, or the local machine) classifies the
  real `stat` mode -- so symlinks, pipes, sockets, and device files are reported as their own
  types and the opt-in `permissions` field shows the mode string -- while a bare volume-backed
  stopped host only distinguishes file vs. directory and leaves `permissions` as `-`. The default
  listing (name, type, size, modified) is unchanged.
- Writing to a stopped host (offline `put`) still works, now through the volume-backed host's
  write interface; `--mode` continues to be ignored when the host is offline.
- Behavior for offline access (which `--relative-to` modes are reachable, the "provider does not
  support volume access" error) is unchanged.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON output call sites to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

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
