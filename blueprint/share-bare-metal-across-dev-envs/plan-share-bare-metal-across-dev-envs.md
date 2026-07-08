# Share a bare-metal box across developer environments

## Overview

- Today each minds dev env keeps its own Neon pool DB, but a bare-metal box's lima slices live in one shared namespace; the per-env reaper deletes any `mngr-slice-*` it doesn't track, so envs sharing a box destroy each other's live slices. This makes box-sharing currently unsafe.
- The root cause is that a slice carries no owner identity and that each env reconstructs box occupancy from its own DB. The fix stamps the owning env into the slice's lima names and derives true occupancy from the box itself.
- Every destructive box operation (reaping, reconcile) becomes scoped to the active env's stamp; legacy un-stamped slices are always left untouched, so the change is backwards-compatible and can roll out incrementally.
- Cross-env over-allocation and port collisions are prevented by a brief box-wide lock that reserves a slot + ports durably (via a no-boot `limactl create`) before the long VM boot, so the lock is held for seconds, not for the whole 10-20 min bake.
- `minds env destroy` gains a slice-teardown step so a destroyed env stops leaking slices, and the connector's existing scheduled sweep gains an env-scoped box-vs-DB reconcile that works correctly in both per-env-DB (dev) and shared-DB (staging/prod) tiers.

## Expected behavior

- Multiple dev envs can bake and run slices on the same bare-metal box without ever deleting, overwriting, or miscounting each other's slices.
- A slice's lima instance and disk are named `mngr-slice-<env>-<hex32>`, where `<env>` is the activated env name; ownership is recoverable from the box alone.
- A slice is "owned by this env" only if its name exactly matches `mngr-slice-<active-env>-<hex32>`. Anything else — another env's stamped slice, or a bare legacy `mngr-slice-<hex32>` — is treated as foreign and is never reaped, deleted, or otherwise touched by this env.
- The orphan reaper deletes only this-env-stamped slices that have no DB row; legacy un-stamped untracked slices are no longer auto-reaped anywhere (a documented one-time cleanup gap, left for manual handling).
- At allocate time the chosen box's free-slot count is derived from the box's actual occupancy (all `mngr-slice-*` data disks present, including other envs' and legacy ones), not from the per-env DB row count, so envs cannot collectively over-subscribe the box.
- Two envs baking on the same box concurrently each acquire a brief box-wide lock to reserve their slot and host ports; a slot/ports reserved by one env are immediately visible to the other, so they never collide on capacity or ports.
- Host ports for a slice are chosen against the union of all existing instances' recorded port-forwards and the box's currently-bound ports, so a reserved-but-not-yet-booted slice's ports are respected and legacy/foreign slices are avoided.
- A bake killed after reserving but before recording its DB row leaves a this-env-stamped slot that the same env's reaper reclaims on its next run; no phantom slot is held permanently, and failed-bake data disks are reliably removed.
- `minds env destroy` tears down every slice the env owns (from its own DB rows) on their boxes before the env is deleted; an already-absent VM/disk counts as success, an unreachable box fails the destroy so nothing leaks silently.
- The connector's scheduled sweep reconciles only its own deployment's stamped slices against its DB in every tier: a stamped slice with no row is reaped, a row with no VM is marked removed/needs-rebake, and foreign/legacy slices are ignored. In shared-DB tiers this is the "divergence = error/alert" behavior; in per-env dev it is automatically correct because foreign slices are skipped.
- Existing OVH VPS pool hosts and any already-baked legacy slices keep working unchanged; nothing about their lifecycle is altered.

## Changes

- Slice naming: derive the lima instance and disk names from both the host id and the active env name (`mngr-slice-<env>-<hex32>`), and add the inverse — a way to test whether a given lima name is owned by a specific env, distinguishing stamped, foreign-stamped, and legacy names.
- Thread the active env name from the env-aware bake entry point through the slice provider configuration so the carve can stamp it; default/unset (legacy callers) continue to produce un-stamped names.
- Occupancy: replace the per-env DB slot count used for the allocate-time free-slot check with a count of the box's actual `mngr-slice-*` data disks, while keeping the per-env `slot_count` as the capacity cap (relying on the operating constraint that all envs register the box identically).
- Reservation + locking: split the carve into a brief locked reservation (count occupancy, choose ports, create the data disk, and `limactl create` the instance so its port-forwards are durably recorded without booting) and the long unlocked boot (`limactl start`); the lock is a single box-wide advisory `flock` held only for the one compound reservation command.
- Port selection: choose the two host ports against the union of all existing instances' recorded port-forwards and the box's bound ports; remove the per-invocation disjoint port-window partitioning, now superseded by the lock plus global port selection.
- Reaper: scope the orphan reaper to delete only this-env-stamped instances/disks with no DB row, never touching foreign or legacy slices; ensure both the reserved-instance and the data disk are reclaimed for abandoned reservations.
- env destroy: add a step to `minds env destroy` that enumerates the env's own slice pool rows and tears down each slice's VM + disk on its box, idempotent on already-absent resources and failing the destroy on an unreachable box.
- Connector reconcile: extend the existing scheduled pool-host sweep with an env-stamp-scoped box-vs-DB reconcile (stamped-mine-no-row → reap; my-row-no-VM → mark removed/needs-rebake; foreign/legacy ignored).
- No database schema change: slice ownership is read from the stamped lima name already stored on the pool row.
- Documentation: note the legacy-untracked-slice cleanup gap and the operating constraint that all envs must register a shared box with identical sizing.
- Changelog: add per-project changelog entries for `libs/mngr_imbue_cloud` and `apps/minds` (and any other project touched).
