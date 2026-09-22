`mngr notify` now asks mngr whether an observer is running instead of working it out for
itself.

mngr gained `is_observe_writer_running`, a supported probe for "does some process hold the
observe lock". `mngr notify` had its own version, which answered the same question by
taking the lock and immediately dropping it again. The shared one is the better probe: it
opens the lock file read-only and never creates it, so probing no longer writes a lock
file into a host dir that has never had an observer, and an observer whose lock file
another user owns is still seen.

A probe that cannot answer at all (a permission problem, say) now raises rather than
quietly reporting "no observer". `mngr notify` says so and starts one anyway, which is the
failure that announces itself: if an observer really was running, the child exits
immediately on the lock and the watcher reports it, whereas assuming one is running would
leave `mngr notify` silently waiting on events nobody writes.
