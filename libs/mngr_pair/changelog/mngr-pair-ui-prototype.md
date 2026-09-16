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
