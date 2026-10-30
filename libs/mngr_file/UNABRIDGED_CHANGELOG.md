# Unabridged Changelog - mngr_file

Full, unedited changelog entries for the `mngr_file` project, consolidated nightly from individual files in `libs/mngr_file/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-02

- The offline-host tests now build their volume-backed host from mngr's generic mock provider instead of the docker provider, which moved into its own plugin. No behavior change.

## 2026-09-21

Witnessed the `mngr file` behavior corpus: every unit in `libs/mngr_file/behaviors/` now has at
least one test linked back to it, and the divergences those tests found in `mngr file` are fixed.

- New witnessing tests under `libs/mngr_file/imbue/mngr_file/witnesses/`, one module per corpus
  area: `addressing/targets_test.py`, `addressing/path_resolution_test.py`,
  `addressing/stopped_hosts_test.py`, `addressing/invariants_test.py`, `listing/listing_test.py`,
  `transfer/reading_test.py`, `transfer/writing_test.py`, and `invariants_test.py` for the
  corpus-wide Rules. Each drives the real `file get`, `file put`, and `file list` commands and
  carries a `witnesses` marker naming the unit it witnesses, with a `partial=` note wherever it
  covers less than the unit states.

- `get` no longer escapes an uncaught error for a missing path or a directory: it establishes what
  is at the path through the host interface before reading, so the refusal is the same stated line
  whether the machine is the running local host, a stopped host served from a filesystem volume, or
  one served by a storage service that raises errors of its own kind
  (`transfer.no-file-at-path`, `transfer.path-is-a-directory`,
  `transfer.directory-refusal-is-machine-independent`, `clean-refusals`).

- `list` refuses a path that names a file instead of reporting it as an empty directory, and a
  missing directory on a stopped host served by a storage service is now refused cleanly rather
  than escaping that service's own error (`listing.missing-directory`, `clean-refusals`).

- `list` classifies a named path before listing it, so listing a file on a stopped host served by a
  storage service is refused with a stated error instead of crashing (`clean-refusals`,
  `resolved-path-reported`).

- `put` refuses writing onto a directory or beneath a file, refuses `--input` naming a directory,
  and its missing-content refusal names both ways of offering content, followed by worked examples
  (`transfer.no-content-offered`, `clean-refusals`).

- Refusal witnesses now require one stated `Error:` line and allow guidance after it, instead of
  requiring the whole refusal to fit on one line (`clean-refusals`, `transfer.no-content-offered`).

- Lifecycle witnesses now read, write, and list with missing files, `--mode`, and `--recursive`, by
  host target and by each agent base, on every storage kind and on running hosts
  (`addressing.never-changes-lifecycle`); resolution witnesses cover absolute paths by host target on
  running and stopped hosts (`addressing.same-resolution-everywhere`).

- The `get` template refusal is also witnessed on a path with no file behind it, so a read before
  the refusal is detected (`template-refused-where-the-outcome-is-content`); put's report test no
  longer claims `named-output-formats`, which the invariants witness covers.

- `get --output` refuses a local destination that is a directory or lies beneath a file, and
  reports the saved local path and the path read as absolute paths
  (`transfer.saved-to-local-file`, `resolved-path-reported`).

- `--relative-to state` on a host target is now a usage error naming the option rather than a
  silently reinterpreted base (`addressing.state-base-needs-an-agent`).

- Test scaffolding added in `testing.py` and `conftest.py`: CLI-discoverable stopped hosts whose
  persisted storage is a filesystem, a storage service, or unreachable (`stopped_host_factory` and
  its `stopped_host`, `service_stopped_host`, and `unreachable_stopped_host` fixtures), a
  `local_agent` fixture, `write_agent_record`, `read_tree`, `InteractiveStdin`, and one
  `addressed_machine_factory` that names the machine a command addresses -- the running local host
  or a fresh stopped host reached through storage -- which replaced three separate copies the
  area branches had each grown.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-17

Added a behavior corpus for `mngr file` at `libs/mngr_file/behaviors/`, specifying the plugin's
externally observable behavior as 48 units across three areas.

- `addressing/` covers turning a target and a path into one absolute path on one machine: which
  base directory each kind of target fixes, how an absolute path bypasses that choice, and what a
  host that is not running changes about all of it.

- `transfer/` covers moving one file's bytes in either direction, including where the local end of
  a transfer sits and what each direction reports.

- `listing/` covers reporting a directory's contents: which attributes an entry carries, which are
  displayed, and why a directory holding nothing and a path holding no directory are different
  answers.

Four corpus-wide invariants bind every unit: refusals are stated errors rather than internal
failures, reports name resolved absolute paths, a write replaces a whole file while a read changes
nothing, and the output formats are a fixed set with no caller-supplied template.

The corpus states the intended behavior, not the current behavior. One known divergence is
recorded in `listing/listing.md`: listing a path where no directory exists is not yet refused
cleanly when the host is stopped and its persisted storage reports a missing path as a
storage-service error rather than an ordinary filesystem error. That note is temporary and is to be
deleted once the code is corrected; the specification itself is not provisional.

Three units were revised after review of the code the corpus specifies. `named-output-formats`
(formerly `fixed-output-formats`) now states that a subcommand whose outcome is a sequence of
records accepts a caller-supplied template, and that one whose outcome is a file's bytes does not;
the corpus previously said no subcommand accepted a template at all. `listing/` gained two
scenarios for template rendering, and `transfer.path-is-a-directory` now requires the refusal to
point at the means of transferring a directory rather than only at the means of listing it, plus a
scenario requiring that refusal to read the same however the machine is reached.

`named-output-formats` was revised again when `mngr file put` gained template support: the Rule
now turns on whether a subcommand describes its outcome with records at all, rather than on whether
there is a sequence of them, and requires a field name shared by more than one subcommand to render
the same way in each. `transfer/` gained a scenario for a write reported through a template.

No production code changed.

Two units were revised after the first witness run exposed problems in their wording.
`clean-refusals` was named "says so in one line", which a witness agent read literally and obeyed by
stripping the worked examples out of `mngr file put`'s no-input refusal. The intent was to outlaw
stack traces, not to cap a refusal's length, so the Rule is renamed and now says explicitly that a
refusal may carry whatever guidance helps the user act on it. `addressing.absolute-path-wins` named
`/etc/hostname`, a file macOS does not have, so its witness could only run on Linux; the scenario now
describes the property that matters -- a file outside every base directory the target admits -- and
leaves the choice of path to whoever witnesses it. Both keep their identity tags, so no coordinate
moves and no witness link breaks.

## 2026-09-11

`mngr file list` and `mngr file put` accept a format template, like the other record-emitting mngr
commands, and render one line per record:

```
mngr file list my-agent --format '{name} {size}'
echo hello | mngr file put my-agent greeting.txt --format '{path} {size}'
```

For `list`, a template can address any field an entry carries -- `name`, `path`, `file_type`,
`size`, `modified`, `permissions` -- not just the columns currently displayed, so `--format` and
`--fields` do not interfere. For `put`, the fields are `path` and `size`, and `size` renders the
same way it does in a listing so that one field name means one thing across the command group.

`get` continues to reject a template: its outcome is the file's own bytes, and a template there
would replace the content asked for rather than describe it.

`mngr file get` now reports the two ordinary addressing mistakes as clean errors instead of
crashing with a Python traceback. Asking for a path that does not exist prints
`Error: No file at <path>. Use 'mngr file list' to see what is there.` Pointing `get` at a directory
prints `Error: <path> is a directory, not a file. Use 'mngr rsync' to transfer a directory, or
'mngr file list' to see what it holds.` Both exit 1, as before; previously each surfaced a raw
traceback from the underlying read. This now holds for a remote host as well as a local one -- see
the `mngr` entry for the SSH-side half of that fix.

`mngr file list` on a directory that does not exist is now an error rather than an empty listing.
It previously printed `(empty)` and exited 0, which was indistinguishable from a directory that
exists and holds nothing. It now prints `Error: No directory at <path>.` and exits 1. A genuinely
empty directory still prints `(empty)` and exits 0.

`mngr file get --output` now reports what it did instead of finishing silently. Saving to a local
file previously produced no output at all, even under `--format json`, so a scripted caller could
not confirm the transfer or learn the resolved remote path. It now prints
`Wrote <n> bytes to <local path>` in `human` format and emits a `file_read` event carrying `path`,
`output_path`, and `size` in `json`/`jsonl`. The event deliberately omits `content_base64`: the
bytes are already on disk at `output_path`, so repeating them would only inflate the event.
Reading to stdout without `--output` is unchanged and still carries `content_base64`.

Corrected three points in the project README, and documented the output-format, `get --output`, and
`list` behavior above:

- The "Target" section claimed that an identifier matching both an agent and a host raises a
  disambiguation error. No such check exists: agent-vs-host is decided from the text of the
  argument alone, with no lookup. The section now describes the actual rule.

- The README told users to write `--output-format`, which is not an option the commands accept.
  The supported spelling is `--format`.

- Added the missing note that a host whose name is a plain word is only reachable as `@NAME`; a
  bare name is read as an agent name and fails to resolve.

## 2026-08-18

# Follow mngr's discovery return type

Updated for mngr's `discover_hosts_and_agents`, which now returns a `DiscoveryOutcome` (hosts and agents, the providers that answered, and the providers that could not be reached) rather than a two-tuple. No behavior change here: target resolution reads the same discovered agents off the outcome.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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
