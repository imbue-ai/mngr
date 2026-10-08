# Host lock safety across SSH reconnects

Status: **proposed.** Follow-up to PR #2350 ("Make remote host-lock acquisition
resilient to transient SSH failures"), which fixed the *acquisition* path. This
spec covers the *hold* phase: keeping a held cooperative lock correct when the
SSH connection drops and is rebuilt mid-critical-section.

**Audience:** developers working on the host layer in `libs/mngr`
(`imbue/mngr/hosts/host.py`, `imbue/mngr/hosts/outer_host.py`) and callers that
run many hosts at once (`libs/mngr_mapreduce`).

**Related:** `libs/mngr/imbue/mngr/hosts/host.py` (`lock_cooperatively`,
`_hold_remote_host_lock`, `_open_remote_lock_channel`, `_build_remote_lock_command`,
`_wait_for_remote_lock_acquired`), `libs/mngr/imbue/mngr/hosts/outer_host.py`
(`_ensure_connected`, `_run_shell_command_with_transient_retry`,
`_translate_ssh_errors`), `specs/boot-loader-resilience`.

## Overview

`Host.lock_cooperatively()` gives a caller exclusive, cross-actor access to a
host for the duration of a `with` block (e.g. all of `mngr create`'s
host-mutating steps). On a **remote** host the lock is a real `flock(2)` held by
a remote shell whose lifetime is bound to a single long-lived SSH channel.

That binding is the problem this spec addresses: **lock ownership is tied to SSH
connection liveness, and nothing re-verifies ownership once the lock is held.**
If the connection drops mid-critical-section, the lock silently releases, and
the per-operation transient-retry machinery silently reconnects and keeps
running the critical section -- now without the lock. Another actor can enter
its own critical section in the gap. The result is a silent mutual-exclusion
violation: two "mutually exclusive" sections running concurrently, with no
exception raised.

The fix is to make a reconnect *while holding the lock* re-establish the lock
and verify that **no other actor acquired it in the interval**, using a
monotonic acquisition counter stored on the host. If no one intervened, the
critical section transparently continues; if someone did (or the lock cannot be
re-established), we raise a new `LockLostError` instead of silently proceeding.

## Background: how the cooperative lock works today

`lock_cooperatively` (host.py:794) branches on `self.is_local`:

- **Local host** -> `_hold_local_host_lock`: a direct `flock(2)` on
  `<host_dir>/host_lock` on the local filesystem. There is no SSH and no
  reconnect, so **local hosts are out of scope for this spec.**
- **Remote host** -> `_hold_remote_host_lock`: the case this spec is about.

The remote lock is a flock held by a remote shell over one SSH channel
(`_build_remote_lock_command`, roughly):

```sh
mkdir -p dir && exec 9>lockfile && flock 9 && \
  printf '%s\n' __MNGR_LOCK_ACQUIRED__ && while IFS= read -r _; do :; done
```

1. The client opens one channel via `transport.open_session()` and runs the
   command. The remote shell grabs `flock` on fd 9, then blocks reading stdin.
2. `_wait_for_remote_lock_acquired` blocks on the client until it reads the
   `__MNGR_LOCK_ACQUIRED__` marker, then the context manager `yield`s into the
   critical section.
3. **Release** = close the channel -> stdin EOF -> the `read` loop ends -> the
   shell exits -> fd 9 closes -> the kernel drops the flock.

### The transport/channel relationship (why this is subtle)

There is **one** paramiko `Transport` (one TCP connection) per `Host`, from a
shared `SSHClient`. Over that transport SSH multiplexes many `Channel`s:

- The **lock** is one long-lived channel on the transport.
- **Shell/file/exec/list operations** run over *other channels on the same
  transport* (pyinfra's `run_shell_command`, the SFTP paths, etc. all use the
  same `self.connector.host.connector.client`).
- **rsync is the exception**: `copy_directory` / `_create_work_dir_via_rsync` /
  `_rsync_paths` / `_rsync_files` shell out to an `ssh` subprocess
  (`run_process_to_completion`), which is a *separate* TCP connection with no
  relationship to the paramiko transport.

Consequences:

- For paramiko operations, lock and ops share one connection, so they normally
  live and die together -- the lock channel cannot silently outlive or predecease
  the op channels *unless the transport itself is replaced*.
- The transport is replaced exactly when the per-operation retry path calls
  `self.connector.host.disconnect()` and reconnects. That reconnect orphans the
  lock channel (it lived on the old, now-dead transport) while ops continue on
  the new transport. **Reconnect is the event that silently voids the lock.**

## The correctness gap

Timeline of the silent violation (A and B are two clients operating on the same
remote host; on a shared host in `mngr_mapreduce`, both are `create`s):

```
t0  A: enters `with lock`  -> A's channel-shell holds flock; A is exclusive owner.
t1  A: runs step_1..step_3 fine.
t2  A: SSH transport drops (e.g. "Connection reset by peer").
        -> A's lock channel dies -> flock RELEASED.
        -> A's Python is mid-block, about to run step_4; that op is stalled, retrying.
t3  B: was blocked on `flock 9`; now unblocks -> B enters its critical section.
t4  A: step_4 goes through _run_shell_command_with_transient_retry, which
        reconnects (new transport) and runs step_4 -- no exception.
        -> A believes it still holds the lock. It does not.
t5  A and B run their "mutually exclusive" sections concurrently. Nothing raised.
```

Two root causes:

1. **Lock lifetime is bound to connection liveness.** A dropped connection
   releases the lock as a side effect of the remote shell dying.
2. **Nothing re-verifies ownership.** A acquires once; its later steps never
   re-check "do I still hold it?" A reconnect restores a *shell*, not the *lock*.

**Note:** PR #2350 only added retry+reconnect to `_open_remote_lock_channel`,
i.e. *acquisition*, which runs before `yield`. At that point no lock is held, so
retrying/reconnecting cannot violate a critical section. #2350 does not introduce
this gap; it is pre-existing, and #2350's purpose (surviving disconnects) makes
disconnects a normal, frequent event, which increases exposure to the gap.

## Design principle: verify no intervening acquisition

We do **not** need the connection to stay up continuously. We only need to know,
after a reconnect, whether **anyone else acquired the lock while we were gone.**
If nobody did, the momentary loss was invisible: the client was not mutating the
host during the gap (its in-flight op was stalled, retrying), so resuming is
indistinguishable from the connection never having dropped.

We detect intervening acquisition with a **monotonic acquisition counter** kept
on the host, incremented once per acquisition under the flock. A holder that
reconnects re-acquires the lock and checks whether the counter advanced by more
than its own single re-acquire. This is a fencing/generation token used for
*continuity verification on reconnect* rather than resource-side rejection.

This is deliberately *less strict* than "never reconnect while locked" (see
[Alternatives considered](#alternatives-considered)). In the target environment
(TMR provisioning, where a given host is provisioned by exactly one `create`, so
contention on that host is essentially nil) almost every blip is uncontended, so
almost every blip should transparently recover instead of failing the `create`.

## Detailed design

### Remote: the acquisition counter

Store a counter in a file beside the lock file:
`<host_dir>/host_lock.generation` (new constant
`_HOST_LOCK_GENERATION_FILENAME`). It must be a **separate** file: the lock file
itself is opened with `exec 9>lockfile`, which truncates it on every acquire, so
it cannot hold durable state.

Extend `_build_remote_lock_command` so that, **after** acquiring the flock and
**before** signalling acquisition, it atomically increments the counter and
reports the new value:

```sh
# ... after `flock 9` (blocking or -n as today) succeeds ...
gen=$(( $(cat genfile 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$gen" > genfile
printf '%s %s\n' __MNGR_LOCK_ACQUIRED__ "$gen"
while IFS= read -r _; do :; done
```

The read-modify-write is serialized because it happens while holding the flock,
so concurrent acquirers cannot race. `_wait_for_remote_lock_acquired` is extended
to parse the integer token that follows the `__MNGR_LOCK_ACQUIRED__` marker and
return it.

### Client: active-lock state

`_hold_remote_host_lock` records, for the duration of the block, an
`_active_lock` on the `Host` (a small private object / dataclass), holding:

- `lock_file_path: Path`
- `generation_file_path: Path`
- `token: int` -- the last acquisition counter value we observed for this lock
- `channel: Channel` -- the *current* lock channel (updatable; a successful
  re-acquire installs a new one)

`_active_lock` is `None` when no cooperative lock is held. We assume the
cooperative lock is **not re-entrant / not nested** on a single `Host` (one held
lock at a time); this matches current usage.

### Re-acquire and verify (the core operation)

When we detect that the lock may have been lost while held (see [Enforcement
points](#enforcement-points)), we run `_reacquire_and_verify_lock()`:

1. Ensure the transport is connected (reconnect if needed).
2. **Fast-fail read (optional optimization):** read the counter file with a
   quick command. If its value `!= token`, someone acquired since we last held
   (or the file was reset) -> raise `LockLostError` without grabbing the flock.
   This avoids blocking behind a long-running other holder in the clearly-lost
   case.
3. Re-open the lock channel (`_open_remote_lock_channel`, reusing #2350's
   transient-retry). This re-runs the lock command, which **blocks** until the
   flock is free, increments the counter, and returns the new value `V`.
4. **Authoritative check:** if `V == token + 1`, no other actor acquired between
   our last hold and this re-acquire (our own re-acquire is the only intervening
   increment). Update `_active_lock.token = V`, install the new channel, and let
   the stalled operation proceed.
5. Otherwise (`V > token + 1`) some actor acquired in the gap -> close the new
   channel and raise `LockLostError`.

The arithmetic is exact: every acquisition increments the counter by exactly 1,
and one of the increments in `(token, V]` is our own re-acquire, so intervening
acquisitions by others `= V - token - 1`. Zero of them iff `V == token + 1`.

Re-acquire uses **blocking** flock semantics regardless of the original
`timeout_seconds`: mid-critical-section we want to either continue or fail, not
time out ambiguously. Step 2's fast-fail is what prevents blocking behind a
current other holder.

### `LockLostError`

Add `LockLostError(HostError)` to `imbue/mngr/errors.py`. It is a `MngrError`,
so existing callers that isolate per-host failures already catch it -- e.g. the
`mngr_mapreduce` launch loop records it as a single failed launch and continues,
exactly as it does for `HostConnectionError` today.

### Enforcement points

Detection must be complete: no critical-section operation may proceed after a
disconnect without first passing `_reacquire_and_verify_lock()`. There is no
single line every operation flows through (paramiko ops and the rsync subprocess
use different backends), so we place checks at these points:

1. **Paramiko reconnect (primary, fail-fast).** Every paramiko operation kind
   funnels into one `*_with_retry` primitive
   (`_run_shell_command_with_transient_retry`, `_get_file_with_transient_retry`,
   `_put_file_with_transient_retry`, `_execute_streaming_ssh_with_retry`,
   `_list_directory_remote_with_retry`), each of which calls `_ensure_connected`
   at the top and again on each retry after `disconnect()`. So **every paramiko
   reconnect passes through `_ensure_connected`.** When `_active_lock` is set and
   `_ensure_connected` would (re)establish a connection, route through
   `_reacquire_and_verify_lock()` instead of a plain reconnect: it either
   re-verifies and continues, or raises `LockLostError`. This catches the common
   case at the first post-drop operation, before it runs.

   **Preserve a live transport while locked.** Reconnection today is *not* limited
   to genuine connection failure. The retry primitives already distinguish
   channel-level from transport-level errors -- `ChannelException` ("channel open
   refused", e.g. server `MaxSessions`) and `SSHException` containing "Channel
   closed" are retried *without* disconnecting, because the transport is still
   alive. But one case, pyinfra's read `TimeoutError`, currently forces a
   `disconnect()` even though (per its own comment) "the connection may still
   appear open." Tearing down a live transport also tears down the live lock
   channel, needlessly releasing a lock we validly hold and opening an avoidable
   window for another actor. Therefore, **while `_active_lock` is set, a retry
   primitive must not `disconnect()` a still-active transport**: check
   `transport.is_active()` first, and on a channel-level or timeout error with a
   live transport, retry the operation on the *same* transport (leaving the lock
   channel intact). Only a confirmed-dead transport (`not transport.is_active()`,
   or a transport-level `SSHException`/`EOFError`/socket-closed `OSError`) should
   trigger the reconnect-and-re-acquire path above. The counter still keeps us
   *safe* if a disconnect does happen; this rule avoids the *unnecessary* loss.

   **Re-entrancy:** `_reacquire_and_verify_lock()` itself calls
   `_open_remote_lock_channel`, which calls `_ensure_connected`. That inner call
   must perform a plain reconnect, not re-route back into re-acquire (which would
   recurse infinitely). Guard with a per-`Host` "re-acquiring in progress" flag:
   `_ensure_connected` only routes through re-acquire when `_active_lock` is set
   *and* the flag is unset. (Initial acquisition is also safe: `_active_lock` is
   set only *after* the first acquire succeeds.)

   **Note:** this is coverage by convention -- each primitive independently calls
   `_ensure_connected`. A future primitive must do the same. The release backstop
   (point 4) is what makes completeness a guarantee rather than a convention.

2. **rsync (separate connection).** rsync bypasses paramiko entirely, so it
   cannot piggyback on point 1. Before each rsync (`_create_work_dir_via_rsync`,
   `_rsync_paths`, `_rsync_files`), when `_active_lock` is set, run the fast-fail
   counter read and confirm the lock channel is still open; if either indicates
   loss, `_reacquire_and_verify_lock()` (or raise). This guards the "lock already
   lost before rsync started" ordering, which is the case a pre-check can
   *prevent*. A paramiko-transport drop *during* a long rsync (rsync runs on its
   own connection and completes unaware) is not prevented here; it is *detected*
   by the next paramiko operation's reconnect check (point 1) or, failing that,
   the release backstop (point 4).

3. **Lock-channel death with a live transport.** SSH multiplexes many channels
   over one transport, so a single channel can close while the transport stays up
   -- the retry primitives already rely on this (the "retry without disconnect"
   cases in point 1). The lock channel is no exception: the remote lock *shell*
   can die (OOM/kill, or a spurious stdin EOF) while the transport is fine. No
   reconnect is triggered in that case, so points 1 and 2 would miss it. Guard by
   checking `_active_lock.channel.closed` (a cheap, non-blocking check) at
   operation boundaries in the retry primitives; if the lock channel is closed but
   the transport is alive, trigger `_reacquire_and_verify_lock()`. (A dedicated
   monitor thread reading the lock channel for EOF is an alternative but is
   heavier; the boundary check is sufficient given operations are frequent within
   a critical section.)

4. **Release backstop (completeness guarantee).** `_hold_remote_host_lock` has a
   single `finally`. Before declaring the block successful, verify the counter is
   still `token` (i.e. no un-recovered intervening acquisition) and the current
   lock channel is open. If not, raise `LockLostError`. Because every exit of the
   context manager passes through this one point, no critical section can ever be
   *reported successful* while the lock was lost, regardless of which operation
   (paramiko, rsync, or a future backend) ran inside. This is detect-not-prevent
   (late), but it is the airtight net beneath the fail-fast checks.

### Composed behavior

The full model, keyed on lock state:

- **Acquiring (no lock held):** reconnect/retry freely (PR #2350). Safe -- no
  critical section exists yet.
- **Holding the lock:** channel-level or timeout errors on a still-active
  transport retry on the same transport, leaving the lock intact. Only a
  confirmed-dead transport (or an observed lock-channel death) triggers
  `_reacquire_and_verify_lock()`. Uncontended -> transparently continue;
  contended or unrecoverable -> `LockLostError`.

## Failure modes and edge cases

| Scenario | Behavior |
|---|---|
| Channel-level error / read timeout, transport still active | Retry on the same transport; lock channel left intact, no re-acquire needed. |
| Transient blip, no other actor acquired | Reconnect, re-acquire, `V == token+1` -> continue. Recovered transparently. |
| Blip, another actor acquired and released in the gap | `V > token+1` -> `LockLostError`. |
| Blip, another actor is *still holding* on reconnect | Fast-fail read sees counter advanced -> `LockLostError` without blocking. |
| Host genuinely unreachable | Re-acquire's reconnect exhausts retries -> `HostConnectionError` (a `MngrError`), isolated per-host by callers. |
| Lock shell dies, transport alive (independent channel death) | Point 3 detects `channel.closed` -> re-acquire-and-verify. |
| rsync ordering (lock lost before rsync starts) | Point 2 pre-check -> re-acquire-and-verify or `LockLostError`. |
| In-flight op partially executed before the drop | Retried on the re-established connection. Relies on the operation being idempotent -- **an assumption that already exists** for today's per-command retry. |
| Counter file missing/reset (host wiped) | Read yields `0 != token` -> `LockLostError`. Correct: a wiped host is not the one we locked. |

### Known limitations and assumptions

- **All lock acquirers must increment the counter.** The design assumes every
  actor that acquires `host_lock` to *mutate* a remote host does so via the SSH
  lock command (and thus the counter). Remote-host operations all go through
  `_hold_remote_host_lock`, so this holds. The in-host idle-shutdown watcher only
  *probes* the flock (non-blocking) and does not hold it for mutation, so it does
  not need to count. **Implementation must confirm** no other in-host actor holds
  `host_lock` for mutation without incrementing; if one exists, it must be updated
  to increment, or documented as a gap.
- **Idle-shutdown during the gap is out of scope.** When the connection drops the
  flock releases, so the in-host idle watcher could shut the host down mid-create
  if its idle timeout elapses. This risk is inherent to connection-bound locking
  and exists regardless of this design; it is not addressed here.
- **Non-nested locks.** `_active_lock` assumes a single held cooperative lock per
  `Host` at a time.

## Alternatives considered

- **Never reconnect while holding the lock.** Simplest: on any transport death
  while locked, raise. Requires no counter, no remote state. Rejected as the
  primary approach because it fails every `create` on every blip even when
  uncontended; in an environment with frequent resets that is a large, avoidable
  loss of resilience. It remains a valid *degenerate* fallback (equivalent to
  treating every reconnect as a lost lock) if the counter mechanism proves too
  costly.
- **Connection-generation counter only (no remote counter).** A purely local
  "transport was replaced" check catches reconnect-orphaning (the common case)
  but misses (a) independent lock-channel death with a live transport and (b)
  rsync's separate connection. It also cannot distinguish contended from
  uncontended reconnects, so it can only ever be as strict as "never reconnect."
  The remote counter is what enables safe continuation.
- **Monitor thread on the lock channel.** A background reader that flips a
  "lock lost" flag on EOF is the most authoritative detector, but adds thread
  lifecycle complexity. The boundary `channel.closed` check (point 3) achieves
  the same coverage given operations are frequent within a critical section.
- **Resource-side fencing tokens.** Classic fencing rejects stale writers *at the
  resource*. Our "resource" is the whole host and operations do not carry a token
  the host could reject, so continuity-verification-on-reconnect (this design) is
  the applicable shape.

## Testing plan

Unit tests (`libs/mngr/imbue/mngr/hosts/host_test.py`), using the existing fake
transport/channel doubles (`_FakeTransport`, `_FakeLockChannel`,
`_FakeHostWithSSH`) extended to serve counter values:

- Uncontended reconnect mid-hold: transport dies, re-acquire returns
  `token + 1`, the block completes successfully; assert the operation resumed and
  no error raised.
- Contended reconnect: re-acquire returns `token + 2` -> `LockLostError`.
- Current-holder-on-reconnect: fast-fail counter read is already advanced ->
  `LockLostError` without a blocking re-acquire.
- Independent lock-channel death (transport still active) -> detected, re-verify
  runs.
- Release backstop: force a lost lock that slips past the per-op checks (e.g. an
  rsync-only section) and assert `_hold_remote_host_lock` raises at exit.
- Counter arithmetic: exercise `_wait_for_remote_lock_acquired`'s token parsing.

## Implementation sequencing

1. `LockLostError` + `_HOST_LOCK_GENERATION_FILENAME`.
2. Counter in `_build_remote_lock_command`; token parsing in
   `_wait_for_remote_lock_acquired` and its callers.
3. `_active_lock` state in `_hold_remote_host_lock`; `_reacquire_and_verify_lock`.
4. Enforcement point 1 (`_ensure_connected` integration) + point 4 (release
   backstop). Land with tests -- this closes the common case.
5. Enforcement point 2 (rsync) and point 3 (independent channel death).
6. Confirm the "all acquirers increment" assumption against in-host actors.
