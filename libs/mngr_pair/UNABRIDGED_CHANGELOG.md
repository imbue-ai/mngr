# Unabridged Changelog - mngr_pair

Full, unedited changelog entries for the `mngr_pair` project, consolidated nightly from individual files in `libs/mngr_pair/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-26

`mngr pair`'s first pass now always ends with a `pair_transferring` event carrying `is_transferring: false`, even when unison found nothing to move ("Nothing to do"). Before, a pairing whose two directories already agreed never said so: `pair_syncing` only means unison is up, and a quiet first pass is not an edge, so a caller could not tell "compared and level" from "still comparing". Human output prints `Up to date.` at that point too.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Threads this plugin starts on a `ConcurrencyGroup` now go through `start_mngr_thread`, which destroys the thread's gevent Hub when it finishes. A thread that ran a host command used to leave its Hub, and the pipe pair it holds, open for the rest of the process's life. A new `test_prevent_raw_start_new_thread` ratchet keeps raw `start_new_thread` calls out.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-16

`mngr pair` is now usable as a supervised subprocess, and reports what it is doing:

- Stopping a sync no longer leaves its `unison` running. The teardown believed the process it watches through was already gone -- CPython marks a thread stopped when a signal handler raises out of `Thread.join`, which is exactly what Ctrl+C did -- so it never signalled `unison`, which kept syncing the paired directories with nothing watching it. Interactively this was masked, because Ctrl+C at a terminal signals `unison` too.

- `SIGTERM` stops a sync the same way Ctrl+C does. Previously it killed the command outright, with the same result.

- `--format jsonl` gained a `pair_syncing` event, emitted once unison is up and watching both replicas, and a `pair_transferring` event carrying `is_transferring`, emitted when unison starts moving bytes and again when it settles. A sync spends nearly all its life up but idle, so "running" and "moving bytes right now" are different facts and a caller showing status wants both.

- `--include` never worked in continuous mode: a replica narrowed by `-path` cannot be watched by `unison-fsmonitor` (it fails every event with "No path was found"), so nothing was ever propagated. Such a sync now polls every couple of seconds instead of watching.

- `mngr pair` can now pair with a host rather than an agent. Naming only `--source-host`, with an absolute `--source-path`, syncs that directory without resolving an agent or requiring one to exist -- a sync is between two directories, and the agent was only ever consulted for git state. Git sync is refused in that mode, and so is a relative source path, since there is no agent work directory to resolve it against.

- `mngr pair` gained the standard `--start/--no-start` flag. It defaults to starting an offline host, as before; `--no-start` makes pairing fail rather than start a machine, for a caller that does not want opening a sync to be what turns a machine on.

- `pair_transferring` now carries `bytes_done` and `bytes_total` while a transfer is in flight, read from unison's own progress narration rather than measured. It costs no extra disk or network work, and it is throttled to about one report a second, since unison rewrites that line many times a second. Human output gains the same numbers: `Transferring... (12.0 MB of 17.0 MB)`.

- Those progress lines are separated by carriage returns rather than newlines, so a single line of unison output can carry several. They are now split apart before being read; previously the whole run looked like one unparsable line.

- `mngr pair` gained `--ignore-archives`, for a caller that has just created one of the two directories. unison keeps an archive per pair of paths; if the directory it describes has been emptied or recreated, unison refuses to run at all rather than propagate what looks like a mass deletion. The flag says there is no shared history to read, which is the truth in that case. Its alternative -- unison's own `confirmbigdel=false` -- would instead let it delete the other replica.

New `--links` / `--no-links`, which decides whether symbolic links inside the two directories are carried across. It defaults to `--links`, so pairing behaves as before; pass `--no-links` when the two sides are different machines, where a link does not mean the same thing on both — an absolute target names a path that need not exist on the other side, and a relative one can point out of the directory, where neither side agrees what it reaches. unison skips them without counting them as failures.

`.git` is no longer excluded unconditionally. It is excluded while the git-reconciling pass is running, which is the mode that owns it — pairing's default, so nothing changes there. A caller that passes `--no-require-git` opted out of that pass, and used to have `.git` dropped anyway from a folder nothing else was reconciling; it now decides for itself, with `--exclude .git` if that is what it wants.

## 2026-09-02

Deflaked the unison symlink sync tests (`test_unison_syncer_syncs_symlinks` and `test_unison_syncer_syncs_directory_symlinks`). Each seeds a symlink whose target is an absolute path inside the source replica, so the link's `exists()` follows it back to the always-present source entry and goes true the moment the link syncs -- even while the real file or directory is still mid-sweep. Each test waited only on that condition, then asserted the real target had synced, so it intermittently failed with the symlink's target missing. Both now wait for the exact terminal state they assert (the real entry present and the link present as a symlink), and the flake marker on the file case is gone.

## 2026-08-26

`mngr pair` now works with agents on remote hosts. Previously it resolved the agent and then refused with "Pairing with remote agents is not implemented yet".

unison is a client/server protocol rather than a one-shot copy, so pairing across a network runs a second unison on the host over mngr's own SSH transport. mngr uses a usable unison that is already installed there, and otherwise installs a pinned static build (2.54.0, checksum-verified) into `~/.mngr/bin` on the host, then runs it to confirm it works there rather than assuming so. Nothing is downloaded on later pairings with the same host.

Both ends need unison 2.52 or newer. This is a real constraint rather than a precaution: versions below 2.52 cannot interoperate with newer ones at all, and Ubuntu 22.04 still ships 2.51. mngr now checks the version rather than just the presence of the binary, and says so clearly when it is too old. Local-only pairing is unaffected and still works with any version, since it runs a single unison process with no protocol negotiation.

The host also needs a `unison-fsmonitor` next to its unison or on its PATH, and mngr checks for that too. The same helper is needed locally, which the requirements docs previously said was unnecessary on Linux; the install hints mngr prints when unison is missing said the same thing, and now point at a release download instead of `apt-get`, which supplies no watcher. Continuous sync watches both replicas, and the far side's watcher runs on the host; without one the pairing completes a single sync and then stops with `Server: No file monitoring helper program found`. The unison packaged by Debian and Ubuntu ships no watcher, so on a host that has only the distro package -- which includes mngr's own agent image -- mngr now installs the static build (which carries both binaries) rather than reporting the distro unison as usable.

The build mngr installs is a static Linux x86_64 binary, so it only installs on such a host: anywhere else -- Linux arm64, for which upstream publishes no binary at all, or a machine that is not running Linux -- mngr reports what is missing instead of installing something that cannot work.

The initial git reconciliation now fetches the agent's branch into your local repository, rather than fetching your repository into the agent's. The old direction used a local filesystem path as a git remote, which a remote host cannot resolve — and on a host where that path happens to exist, it would have named a different repository. As a side benefit for local agents, the write now lands in the repository you own instead of in the agent's object store.

When you pair a subdirectory of an agent (`mngr pair my-agent:/subdir`), the reconciliation fetch now names the agent's repository root rather than the subdirectory, which is not a repository git can fetch from.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-29

Fixed unbounded memory growth in `mngr pair`. Unison runs in continuous-sync mode for the whole pairing session and narrates every transfer, and all of that output was retained for the life of the session.

The unison process is now started with `is_output_accumulated=False`, so its output is logged as it arrives and then dropped.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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
