# Plan: host_backup snapshot rotation (fix empty gVisor backups)

## Refined prompt

let's chat about option A and the mitigations--the issue is that we cannot easily clean up old snapshot paths if they're just random ids. Instead let's use the time of the snapshot as the request id. Then we can just remove really old snapshots (if we have more than, say, 5 of them). Also, we cannot simply "fail" on a 0 byte snapshot -- that's a totally possible thing that could happen (if nothing has changed since the last time)

* Scope the change to the `outer_trigger` (docker-vps / gVisor) path **only**; leave `btrfs_local` (lima) and `direct` (plain docker) completely unchanged
* Keep each snapshot taker self-contained (no shared refactor); the runner stays method-agnostic and delegates a post-backup cleanup step to the taker — `OuterTriggerSnapshotTaker` implements keep-N GC, the others keep delete-after-each
* Keep the newest **N** local btrfs snapshots by count (not by age) for `outer_trigger`; N is an `outer_trigger`-only knob `max_local_snapshots` in `backup.toml`'s `[snapshot]` (default 5), ignored by lima/direct
* Reuse the existing `_iso_now()` microsecond ISO timestamp verbatim as both the request id and the snapshot directory name (e.g. `snapshots/2026-06-12T03:43:57.123456Z`)
* The in-container `host_backup` enumerates existing snapshots via `readdir` of the read dir directly and trusts it — only same-path inode swaps were broken; parent listing and fresh-name reads are reliable
* Per-tick order: create `snapshots/<ts>` → restic backup it → GC oldest beyond N; GC runs every tick **except** when the snapshot create itself failed; restic `forget`/`prune` still only after a successful backup
* Old-snapshot deletion is targeted by name via a sensibly-named request field; the helper validates the target is a direct child (basename only; reject `/` and `..`) of `<btrfs_mount>/snapshots/` before `btrfs subvolume delete`, treating "already gone" as success
* Don't rename `SnapshotSettings` fields; `outer_trigger` derives the snapshots dir from the parent of the existing path fields and appends `<ts>` at runtime (zero edits to lima/direct)
* Distinguish failure from a legitimate no-change backup using **only** restic's exit code (exit 0 = success even when nothing was added; exit 3 / read errors = failure) — no byte-size guard
* On a name collision (`snapshots/<ts>` already exists) the create fails and the tick fails; the next tick uses a fresh `<ts>`
* GC ignores entries whose names don't parse as a valid timestamp; GC runs only as part of ticks (no separate startup pass)
* Reuse the existing `SNAPSHOT_DELETED` event per deleted snapshot; no new event type
* No new regression test; rely on existing tests
* Target new hosts only; no back-compat for already-running leased hosts

---

## Overview

- Root cause: on vps-docker hosts the workspace runs under gVisor (`runsc`); restic reads the backup snapshot through the gofer (9p). The backup reused one fixed btrfs subvolume path (`/mngr-btrfs/snapshots/current`), deleting + recreating it every cycle. After the first cycle the gofer holds a stale handle to the deleted subvolume, so restic reads an empty dir (`readdirent … no such file or directory`, exit 3) and commits a 0-byte snapshot. Only the first backup after boot ever captures data.
- Fix (Option A): give each snapshot a unique, time-named directory (`snapshots/<iso-timestamp>`) so every read is a fresh-name lookup — the path that already works. Never reuse a path across a delete+recreate.
- Retention: instead of delete-after-each, keep the newest **N** snapshots (default 5) and GC the rest by count. Time-named dirs make "oldest" trivially sortable and cleanable.
- Failure signal: rely solely on restic's exit code. A clean no-change backup (exit 0, full restore-size, 0 bytes *added*) is a success; only restic read errors (exit 3) are failures. No byte-size guard is added.
- Scope is deliberately narrow: only `outer_trigger` changes. `btrfs_local` (lima) and `direct` (plain docker) are untouched, and the three snapshot takers stay self-contained.
- Two repos: the outer helper lives in the mngr monorepo (`libs/mngr_vps_docker`); the inner backup loop and bootstrap live in forever-claude-template (`libs/host_backup`, `libs/bootstrap`). The helper reaches workspaces via FCT's `vendor/mngr`. New hosts only — existing leased hosts keep the old behavior until recycled.

## Expected behavior

- On a vps-docker workspace, each backup tick creates a new read-only btrfs snapshot at `<btrfs-mount>/snapshots/<timestamp>`, restic backs it up, then snapshots beyond the newest N are deleted.
- Consecutive backups all capture real data (not just the first post-boot one) — the stale-gofer-handle failure no longer occurs because paths are never reused.
- The on-host btrfs snapshots dir holds at most `max_local_snapshots` (default 5) snapshots at any time; older ones are pruned automatically.
- A tick where nothing changed since the last backup still succeeds (restic exit 0); it is not treated as a failure.
- A tick where the snapshot *create* fails (e.g. name collision) fails cleanly, runs no GC, and the next tick proceeds with a fresh timestamp.
- GC runs every tick whose snapshot create succeeded, regardless of whether the restic backup itself succeeded — so a run of restic failures cannot let snapshots accumulate past N.
- `restic forget` / `prune` (the remote-repo retention) still run only after a successful restic backup, unchanged.
- Each deleted snapshot emits a `SNAPSHOT_DELETED` event (now possibly several per tick); no new event types.
- lima (`btrfs_local`) and plain-docker (`direct`) behavior is byte-for-byte unchanged.
- Restic repositories stop accumulating 0-byte snapshots on new hosts. (Pre-existing empty snapshots in current repos are out of scope and left as-is.)

## Implementation plan

### mngr monorepo — `libs/mngr_vps_docker`

- `imbue/mngr_vps_docker/resources/snapshot_helper.sh` (modify):
  - Remove the fixed `SNAPSHOT_PATH=…/snapshots/current` constant; compute the target per request.
  - `handle_request`: parse an additional optional `target` field from `request.json` (via `jq`).
  - `do_snapshot`: create the snapshot at `${MNGR_BTRFS_MOUNT_PATH}/snapshots/${request_id}` (the request id *is* the timestamp name). Remove the defensive pre-delete — if the path already exists, let `btrfs subvolume snapshot` fail naturally (non-zero exit → surfaced to the caller). Return the created path in `snapshot_path`.
  - `do_cleanup`: delete the snapshot named by `target`. Validate `target` is a non-empty pure basename (reject any `/` and `..`); construct `${MNGR_BTRFS_MOUNT_PATH}/snapshots/${target}`; `btrfs subvolume delete` it; treat "not present" as success (exit 0). Reject a missing/invalid `target` with a non-zero exit + clear stderr.
  - `emit_result` shape unchanged (`request_id, operation, exit_code, stdout, stderr, snapshot_path`).
- `imbue/mngr_vps_docker/_snapshot_helper_test.py` (modify): update to cover per-name create, name collision failure, targeted cleanup, basename validation (reject traversal), and "already gone" cleanup success.
- No change to `container_setup.py` / `instance.py`: the mount points (`/mngr-snapshots:ro`, `/mngr-snapshot:rw`) and provisioning are unchanged; only the protocol payload + helper logic change.

### forever-claude-template — `libs/host_backup`

- `src/host_backup/config.py` (modify):
  - `SnapshotSettings`: add `max_local_snapshots: int = Field(default=5, …)` (used only by `outer_trigger`; ignored by other methods). Note in the field description that for `outer_trigger` the `snapshot_current_path` / `snapshot_read_path` are treated as the snapshots *directory* (the per-cycle `<timestamp>` is appended at runtime).
  - `_snapshot_to_toml_table`: serialize `max_local_snapshots`.
  - `render_default_backup_toml`: include `max_local_snapshots` in the rendered `[snapshot]` default.
- `src/host_backup/snapshot.py` (modify):
  - `SnapshotTakerInterface`: add abstract `cleanup_after_backup(self) -> tuple[str, ...]` returning the snapshot identifiers deleted (for `SNAPSHOT_DELETED` events). Keep `take_snapshot` and `delete_snapshot`.
  - `OuterTriggerSnapshotTaker`:
    - `take_snapshot`: remove the leading `self.delete_snapshot()` call. Set `request_id = _iso_now()`; call `_do_request("snapshot", request_id)`. Compute `read_path = <read_dir>/<request_id>` where `<read_dir> = settings.snapshot_read_path` (now the directory). Return `SnapshotResult(snapshot_path=<helper path>, read_path=…)`.
    - `cleanup_after_backup`: `readdir` `<read_dir>`; keep only entries whose names parse as a valid `_iso_now()` timestamp (ignore others); sort; if count > `settings.max_local_snapshots`, send a `cleanup` request (with `target=<name>`) for each of the oldest surplus entries; return the deleted names.
    - `_do_request`: extend the request payload to include `target` when provided (snapshot requests omit it).
    - `delete_snapshot`: retained only to satisfy the interface; no longer used in the `outer_trigger` flow (or removed if the interface no longer requires it — see Open questions).
  - `BtrfsLocalSnapshotTaker` (unchanged behavior): implement `cleanup_after_backup` as a thin wrapper that calls its existing `delete_snapshot()` (delete `current`) and returns that path. No other changes.
  - `DirectSnapshotTaker` (unchanged behavior): `cleanup_after_backup` returns `()`.
- `src/host_backup/runner.py` (modify):
  - `_cleanup_snapshot`: call `taker.cleanup_after_backup()` instead of `taker.delete_snapshot()`; emit one `SNAPSHOT_DELETED` event per returned name (or a single event when none/one, preserving the current event shape). The existing control flow already skips this step when `take_snapshot` returned `None` (create failed) and already runs it in a `finally` regardless of restic success — matching "GC every tick except when create failed."
- `src/host_backup/restic.py`: no change (exit-code-only success/failure already implemented in `_run_restic_backup`).
- `src/host_backup/events.py`: no change (reuse `SNAPSHOT_DELETED`).

### forever-claude-template — `libs/bootstrap`

- `src/bootstrap/manager.py` (modify): in the `OUTER_TRIGGER` branch of the snapshot-settings probe, set `snapshot_current_path` / `snapshot_read_path` to the snapshots **directory** (`/mngr-btrfs/snapshots` and `/mngr-snapshots`, dropping the `/current` suffix) and set `max_local_snapshots=5`. Leave the `BTRFS_LOCAL` and `DIRECT` branches untouched.

### Cross-repo / rollout

- Sync the updated `snapshot_helper.sh` into FCT's `vendor/mngr` via the existing vendor flow (`just sync-vendor-mngr` / release-minds) so newly-baked pool hosts ship the new helper.
- New hosts only; no migration for existing leased hosts.

## Implementation phases

1. **Outer helper (mngr repo).** Update `snapshot_helper.sh` for per-name create + targeted, validated cleanup; update `_snapshot_helper_test.py`. Self-contained; the old fixed-`current` callers still work until the inner side changes (the snapshot op accepts any request id).
2. **Config knob (FCT).** Add `max_local_snapshots` to `SnapshotSettings` + the toml writer/renderer. No behavior change yet.
3. **Inner taker (FCT).** Rework `OuterTriggerSnapshotTaker` for timestamped names + `cleanup_after_backup` GC; add the interface method; implement the trivial wrappers for `BtrfsLocal`/`Direct`. After this the inner side produces unique names and enumerates for GC.
4. **Runner wiring (FCT).** Switch the post-backup step to `cleanup_after_backup` and emit per-name `SNAPSHOT_DELETED`. End-to-end keep-N behavior now works for `outer_trigger`.
5. **Bootstrap paths (FCT).** Point the `OUTER_TRIGGER` `SnapshotSettings` at the snapshots directory + default `max_local_snapshots`. Fresh boots now write the correct `[snapshot]` section.
6. **Vendor + verify.** Sync the helper into FCT `vendor/mngr`, bake/lease a fresh gVisor host, and verify multiple consecutive non-empty backups with at most N retained snapshots.

## Testing strategy

- Per the decision, **no new automated regression test** is added; rely on existing tests plus manual verification. (This leaves the exact gofer-staleness failure uncovered by CI — see Open questions.)
- Update existing tests that the changes touch:
  - `_snapshot_helper_test.py` (mngr): per-name create; create fails when the target already exists; cleanup deletes by `target`; cleanup rejects names containing `/` or `..`; cleanup of an absent name returns success.
  - `host_backup` unit tests (FCT): `SnapshotSettings` round-trips `max_local_snapshots`; the toml renderer includes it; `OuterTriggerSnapshotTaker.cleanup_after_backup` keeps newest N and ignores non-timestamp entries (exercise with a fake/stubbed helper request layer where one exists, or a `direct`/local seam).
- Manual verification on a fresh gVisor (vps-docker) host:
  - Create a workspace; let ≥2 backup ticks run; confirm via `restic snapshots` that each successive snapshot is non-empty and readable (the regression that previously produced 0-byte snapshots).
  - Confirm `<btrfs-mount>/snapshots/` retains at most `max_local_snapshots` time-named subvolumes and prunes the oldest.
  - Confirm a no-change tick reports success (restic exit 0), not failure.
- Edge cases to exercise manually: name collision → tick fails, next tick recovers; snapshot-create failure → GC skipped that tick; restic backup failure → GC still runs; non-timestamp entry present in snapshots dir → ignored by GC.
- Confirm lima (`btrfs_local`) and plain-docker (`direct`) behavior is unchanged (their existing tests still pass untouched).

## Open questions

- **No automated regression coverage (per 13d).** The precise failure (gofer serving a stale handle for a reused path) cannot be reproduced in unit tests and won't be caught by CI. Risk: a future refactor reintroduces path reuse silently. Worth reconsidering a lightweight deployment/release test asserting two consecutive non-empty `outer_trigger` backups.
- **Field-name semantics (per 18a).** `snapshot_current_path` / `snapshot_read_path` now hold a *directory* for `outer_trigger`, which is mildly misleading. Accepted to avoid touching lima/direct; a later rename to `snapshot_dir` / `snapshot_read_dir` could clean this up.
- **`delete_snapshot()` on the interface.** With `outer_trigger` no longer using it, decide whether to keep it (still used by `btrfs_local`) as-is or fold cleanup entirely into `cleanup_after_backup`. Leaning: keep it (lima uses it internally).
- **Colons in path names.** `_iso_now()` produces names like `2026-06-12T03:43:57.123456Z` (contains `:`). Valid on Linux/btrfs and handled by restic, but the helper must quote paths carefully. Alternative: a colon-free format — explicitly rejected (3a chose verbatim `_iso_now()`), noted only as a latent footgun.
- **Serial cleanup RPCs.** GC sends one `cleanup` request per surplus snapshot over the single request/result file, serially. Fine at N=5, but a large backlog (e.g. after a long outage) would take several round-trips in one tick.
- **Existing hosts / existing empty snapshots.** Out of scope (new hosts only). No migration re-provisions the helper on running hosts, and pre-existing 0-byte restic snapshots in current repos are left untouched.
