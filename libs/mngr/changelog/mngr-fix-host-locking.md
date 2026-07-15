Made the remote cooperative host lock safe across SSH reconnects. Previously a dropped connection mid-critical-section silently released the `flock`, and the transient-retry machinery reconnected and kept running unlocked -- a silent mutual-exclusion violation that let another actor enter its critical section in the gap.

- A monotonic acquisition counter (`host_lock.generation`) is now incremented under the lock on every acquire. A holder that reconnects while holding the lock re-acquires and verifies no other actor acquired in the gap: if the counter advanced by only its own re-acquire it transparently continues, otherwise it raises the new `LockLostError` (a `MngrError`, already isolated per-host by callers such as the `mngr_mapreduce` launch loop).

- Loss is detected at every boundary: the paramiko reconnect chokepoint, an independently-dead lock channel, before each rsync (which uses a separate connection), and a single-exit release backstop that guarantees no critical section is reported successful while the lock was silently lost.

- While a lock is held, a still-live SSH transport is no longer torn down on a channel-level or read-timeout error (which would needlessly drop the lock); only a confirmed-dead transport triggers reconnect-and-re-verify.

- All in-host lock acquirers now participate in the counter so an in-host acquisition is visible to a reconnecting remote holder: the idle-shutdown watcher increments the counter (and holds the lock) when it takes the host down, and the local-filesystem lock path increments on acquire.
