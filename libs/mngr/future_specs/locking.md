## Locking

- Commands require exclusive access via locking [future] if and only if they are modifying the *state* of a host or agent
- Lock files count as activity for idle shutdown detection [future]
- Commands that affect multiple agents/hosts must specify the behavior when all matches cannot all be locked (continue-and-warn, fail immediately, or retry-until-locked [future])

In particular, this means that the following commands require locking:

- create
- start
- stop
- destroy
- cleanup
- clone
- migrate
- provision
- limit
- rename

While operations like push and pull are clearly modifying their targets, locking is not required because they are not modifying the *state* directory of the host or agent (just the working directory).
For such commands, see the [multi-target](../generic/multi_target.md) options for behavior when some agents cannot be processed.

## Deployment Locking and Idle Detection Coordination

It is ideal to avoid concurrent access from multiple instances of mngr to a host/agent while deployment commands are running. This prevents race conditions and state corruption during critical operations.

### Mechanism (implemented)

A single cooperative host lock coordinates all state-changing operations on a host. It is a real `flock(2)` on the host's `host_lock` file, held by `Host.lock_cooperatively`:

1. **Acquire**: A state-changing command (create, start, gc, ...) acquires the lock for the duration of its critical section. On local hosts this is a direct `flock(2)`; on remote hosts it is a `flock(2)` held over a long-lived SSH exec channel. Because both holders take a genuine `flock(2)` on the same `host_lock` inode, a holder running locally inside the host (e.g. a VM/container boot hook) and a holder running remotely over SSH (e.g. the desktop client) mutually exclude.

2. **Idle detection**: The in-host idle-shutdown watcher tests the lock with a non-blocking `flock` probe (not file existence -- the lock file persists after release so its inode stays stable). If the lock is held, the watcher does not shut the host down. Holding the lock therefore also suppresses idle shutdown for the duration of an operation.

3. **Release**: The lock auto-releases when the operation's block exits, even on error. On remote hosts the SSH channel closes, the remote shell exits, the fd closes, and the lock releases -- so a crashed/interrupted controller never leaves a stale lock.

4. **Timeout / blocking**: `create` and `start` block indefinitely until the lock is acquired (a contended operation waits rather than failing); `gc` and other callers use a bounded wait and raise `LockNotHeldError` on timeout (over SSH via `flock -w`).

5. **Concurrent attempts**: Because the operations share one lock, they serialize. After a `gc` that destroyed a host/agent releases the lock, a waiting `start` re-checks that the target agent's state directory still exists and fails with a clear not-found error rather than booting a removed agent.

### Crash Recovery

- A crashed/interrupted controller releases the lock automatically (channel close / process exit), so no stale lock is left behind in the common case.
- As a backstop, the idle-shutdown watcher enforces a hard maximum host age, so a host shuts down eventually regardless of lock state.
- For debugging, `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` launches a detached on-host process that keeps holding the flock after a failed remote create, so the host stays up (bounded only by the hard max-age) for inspection.

### Benefits

This mechanism provides:
- Prevention of concurrent state changes to the same host (true cross-actor mutual exclusion)
- Coordination between operations and idle detection via a single lock
- Automatic release on crash/interruption (no stale locks), with a hard max-age backstop
- Clear indication when a host is unavailable due to an ongoing operation

