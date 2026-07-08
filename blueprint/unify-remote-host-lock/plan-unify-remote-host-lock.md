# Unify the remote `mngr start` lock with the normal remote host lock

## Overview

- Today there are two host locks: the general `lock_cooperatively` (the "normal host lock") and a newer `lock_for_starting` (the "mngr start lock"). They use different remote strategies and never should have diverged.
- The normal lock's remote path is weak: it just writes/removes a `host_lock` marker file (whose existence the idle-shutdown watcher reads) and provides no real cross-actor mutual exclusion. The start lock's remote path is a genuine `flock(2)` held over an SSH channel.
- Goal: remove `lock_for_starting` and have `mngr start` use the one unified `lock_cooperatively`, upgraded to use the start lock's real-`flock`-over-SSH strategy for remote hosts.
- This makes `start`, `create`, and `gc` share a single lock and mutually exclude — which closes a real race (gc tearing down a host while a start boots it) via a lightweight post-acquire existence check.
- `flock` becomes a first-class required remote dependency (like `git`/`sshd`/`jq`), bootstrapped on every provider's agent host.

## Expected behavior

- `start`, `create`, and `gc` all contend for one host lock; only one runs against a given host at a time.
- A contended `create` or `start` now **blocks indefinitely** until it acquires the lock (previously `create` could time out with `LockNotHeldError`); `gc`/`list`/other callers keep a finite 300s timeout that still raises `LockNotHeldError` on expiry.
- While waiting on a contended lock, mngr emits a "waiting to acquire host lock…" message so an indefinite wait doesn't look like a hang.
- Remote locking is now true mutual exclusion across actors (e.g. the minds desktop client over SSH vs. an in-host VM/container boot hook), not just an advisory marker file.
- If `gc` destroys a host/agent, a `start` that was serialized behind it acquires the lock, finds the agent's state directory gone, and fails with a clear, expected not-found error (noting it was likely garbage-collected) instead of trying to boot a dead host.
- On a failed remote `create` with `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1`, the host stays alive for debugging: a detached holder process keeps the flock held so idle-shutdown is suppressed (indefinitely, bounded only by host max-age or manual destroy). Without the flag, the lock auto-releases on teardown and the host idle-shuts-down normally.
- Idle-shutdown decisions, `mngr list` lock columns, and `is_lock_held` all reflect the *actually held* flock (not mere file existence), so a stale/never-deleted lock file no longer keeps a host alive forever.
- A transient SSH drop while the lock is held fails the in-flight operation (the lock releases) rather than silently continuing.
- A missing `flock` binary on a host is treated as a "should never happen" condition and surfaces as an unexpected error, not a swallowed timeout.
- Standard Debian-based images already ship `flock`, so for nearly all users this adds no install step; minimal/custom images get it via the bootstrap fallback.

## Changes

### Lock interface and implementation (`libs/mngr`)
- Remove `lock_for_starting` from the host interface (`interfaces/host.py`) and its implementation plus helpers (`_hold_remote_start_lock`, the `host_start_lock` file, the start-lock marker constants) from `hosts/host.py`.
- Upgrade `lock_cooperatively`'s remote path to hold a real `flock(2)` over an SSH channel on the single `host_lock` file (the never-deleted, inode-stable strategy the start lock used), replacing the write/remove marker-file approach.
- Keep `lock_cooperatively`'s local path as-is (real `fcntl` flock with poll + timeout).
- Support a timeout on the remote path via `flock -w`, mapping expiry to the expected `LockNotHeldError`; treat a missing `flock`/lock-command failure as a distinct, unexpected error that propagates.
- Allow callers to request an indefinite (blocking) acquire; `create` and `start` use it, while `gc`/other callers pass the finite 300s timeout.
- Emit user-facing feedback when the lock is not immediately available.

### Lock-state detection
- Change `is_lock_held` (remote) and `get_reported_lock_time` to reflect a real held-flock test rather than file existence/mtime.
- Update the in-host idle-shutdown watcher (`resources/activity_watcher.sh`) to detect the lock via a non-blocking `flock` held-test instead of `host_lock` file existence.
- Update the listing collection script(s) (`providers/listing_utils.py`) so the lock-held signal is gathered via the flock test, folded into the existing batched round-trip (replacing the `LOCK_MTIME` stat-based signal).

### Call sites
- Point `mngr start` (`cli/start.py`) at `lock_cooperatively` (indefinite) instead of `lock_for_starting`, preserving its serialize-then-no-op-if-already-running behavior.
- Have `create` (`cli/create.py`, `api/create.py`) acquire the lock with an indefinite timeout.
- Keep `gc` (`api/gc.py`) on the finite timeout.
- Add a lightweight post-acquire check in `start`: verify the target agent's state directory still exists; if not, fail with an existing not-found error (e.g. `AgentNotFoundError`) explaining it was likely garbage-collected.

### Debug retention
- Replace the marker-file retain/remove logic for `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` with a detached (`nohup`'d) on-host holder process that keeps the flock held indefinitely on failed remote creates; remote hosts only. Keep the existing failed-host-teardown-skip behavior in `api/create.py`.

### `flock` as a required dependency
- Add `util-linux` (binary `flock`) to the shared `REQUIRED_HOST_PACKAGES` (`providers/ssh_host_setup.py`), which flows to the docker default Dockerfile, docker/modal/vps/imbue-cloud container bootstrap automatically.
- Add `util-linux`/`flock` to the duplicated lists: the prebuilt image `resources/Dockerfile` and the Lima provision script (`mngr_lima/lima_yaml.py`).
- Add `util-linux` to the forever-claude-template (FCT) Dockerfile's dependency-install script, via an external worktree of forever-claude-template under `.external_worktrees/` (so the prebuilt minds image ships it and avoids the bootstrap-fallback warning).
- Do **not** add `flock` to the local-machine dependency preflight (`utils/deps.py`) or to the outer-box (dockerd host) package lists.
- Update the install-snippet assertions in the affected host-setup tests.

### Docs and tests
- Update `architecture.md` (cooperative locking is no longer "[future]") and align `future_specs/locking.md` with the unified flock-based host lock.
- Re-point the existing cross-actor start-serialization tests at the unified `lock_cooperatively` so the mutual-exclusion guarantee stays covered; add coverage for the gc-vs-start existence check and the timeout-vs-missing-flock distinction.
- Add changelog entries for each touched project (at minimum `libs/mngr` and `libs/mngr_lima`).

### Out of scope
- Mixed-version coexistence during rollout (an older mngr using the legacy lock file while a newer one uses the unified lock) — no compatibility shim.

✓ Explore  ✓ Plan  ● Write  ○ Refine
