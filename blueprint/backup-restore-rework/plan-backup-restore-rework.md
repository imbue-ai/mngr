# Backup restore rework

## Overview

- Finish the contributed workspace-backups stack (merged onto `mngr/finish-backups-branch`, PR #2583) by reworking its riskiest piece — the staging+swap in-place restore — and fixing the review findings around it. None of the staging+swap code has shipped, so it is deleted outright with no compatibility concerns.
- The restore becomes an in-place sync: `restic restore <snapshot>:<subpath> --target /mngr --delete` with the default `--overwrite if-changed`. This eliminates the double-disk staging copy, only rewrites files that actually changed (faster, less I/O), and makes a failed restore convergent — re-running it finishes the job instead of finding a half-deleted host dir.
- Restic 0.18.1 (already the version minds bundles, with recorded sha256s) becomes the floor everywhere: the restore script auto-downloads and persistently installs the pinned binary when the workspace's restic is older than 0.17, and the default-workspace-template image pins it for new workspaces (separate dwt PR, same branch name, via `just dwt-worktree`).
- Every failure mode found in review gets a deliberate answer: stale restic locks (unlock + retry), broken chat gate after a failed restore (explicit "Force restore"), orphaned tick journal entries (self-healing check), failed safety snapshot ("Restore without backing up first"), drifted workspace credentials (canonical env reinjected before dispatch), and rolled-back backup-service code (chained update, on by default).
- Users can follow along: all tracked backup operations stream their real output live into a toggleable details panel (mirroring the workspace-creation details toggle), with full history replayed to any page that attaches mid-operation.

## Expected behavior

### Restore flow

- Clicking Restore opens the existing confirmation dialog, now with an "Update the backup service afterwards" checkbox, checked by default.
- A confirmed restore runs as one tracked operation: gate on chats/ticks (cancellable), pre-restore safety snapshot, in-place sync restore, `restic.env` write-back, `restored`/`restored-from:<time>` snapshot, `uv sync`, service restart — then, if the checkbox was on, the existing idempotent backup-service update converge in the same operation.
- The in-flight restore still reports on its own table row ("Restoring..." with Cancel beside it, withdrawn at the point of no return); the timeline afterwards shows the pre-restore safety snapshot and the "Restored from <time>" entry as before.
- Disk usage during restore is bounded by the delta between current state and the snapshot — there is no staging copy; unchanged files are not rewritten or re-downloaded.
- A workspace whose restic is older than 0.17 gets the pinned, sha256-verified restic 0.18.1 downloaded (amd64 and arm64) and installed persistently, shadowing the apt binary, before restoring; this happens at most once per workspace and the whole workspace (including the hourly host-backup service) converges on the pinned version.
- If the chained update fails after a successful restore, the operation still completes successfully, with a warning notice naming the update failure and pointing at the "Update backup software" button.

### Failure and retry behavior

- A restic lock left by a tick the restore's `stop all` killed is handled automatically: on a lock error, the script runs `restic unlock` and retries once (host_backup's proven pattern).
- If the safety snapshot fails, the operation fails with that reason and the notice offers "Restore without backing up first"; clicking it re-dispatches immediately with the safety snapshot skipped entirely (no re-confirmation — it is a retry affordance, like "Stop chats and try again").
- If the gate probe cannot determine running chats (e.g. the workspace was broken by an earlier failed restore), the failure notice offers "Force restore", which re-dispatches immediately with the chat gate skipped. This is never automatic.
- The tick-in-flight check is self-healing: if the host-backup supervisord program is not RUNNING, no tick can be in flight, so orphaned `BACKUP_STARTED` journal entries (from a tick killed mid-flight) no longer block operations.
- Re-running a restore that failed midway converges: the in-place sync picks up where things stand, and already-restored files are skipped.

### Operation visibility and cancellation

- Every tracked backup operation (restore, update, storage change) gets a toggleable details panel — collapsed by default, like the workspace-creation details toggle — showing the operation's full streamed output (script phases plus throttled restic progress), so a stuck operation is distinguishable from a slow one.
- The full output history is stored on the operation (size-capped) and replayed on attach: a page opened mid-operation, or a second window, sees the same complete log.
- Cancelling a still-waiting operation ends it in a new CANCELLED terminal state, rendered as a neutral notice ("Restore cancelled. Nothing was changed.") instead of a red error.
- The workspace's `restic.env` is converged to the canonical copy before the restore is dispatched (a differing workspace copy is archived aside, as today), so restore works on workspaces whose env was lost or drifted.

### API and tests

- `GET /api/v1/workspaces/<id>/backups` returns 400 for any `limit`/`offset` that is not a non-negative integer (today, garbage silently means "all"/"0").
- The sync e2e's backup-download step navigates to settings once and polls without reloading, so the async snapshot fetch can actually complete.
- A new end-to-end restore test runs in the per-push snapshot-resume tier: real baked Docker workspace, local restic repo, real product restore path (worker, script, registry), asserting a sentinel file rewinds, the `restored` tag appears, and services come back RUNNING.
- ci.yml's manual dispatch gains a test-filter input so a single snapshot-tier test (e.g. the sync e2e) can be dispatched against a branch.

## Changes

### Workspace restore script (`backup_workspace_scripts.py`)

- Rewrite `BACKUP_RESTORE_SCRIPT` around in-place `restic restore <id>:<subpath> --target <host_dir> --delete`; delete the staging dir, swap loop, nested-`host_dir/` probing, and stale-staging workaround (the desktop passes the resolved subpath in).
- Add a restic version check with pinned-download-and-persist fallback (0.18.1, amd64 + arm64 sha256s; GitHub releases, which workspaces already reach for backup updates).
- Add `restic unlock` + single-retry wrapping to the script's restic invocations.
- Read safety-snapshot excludes from the current `runtime/backup.toml` (tolerant parse, defaults as fallback); always exclude nothing extra beyond that (staging exclude becomes moot).
- New argv flags: skip the safety snapshot; skip the chat gate. Both only ever set by the desktop in response to an explicit user retry action.
- Make the shared tick-in-flight check consult `supervisorctl status host-backup` (service down ⇒ no tick possible).
- Stream progress: emit phase lines and throttled restic `--json` progress on stdout as the script runs (final marker JSON line unchanged).

### Desktop-side worker and operations (`backup_update.py`, `workspace_operations.py`, `backup_provisioning.py`)

- Resolve the snapshot subpath (root vs nested `host_dir/`) desktop-side via the bundled restic's `ls`, alongside the existing snapshot resolution; pass it to the script.
- Reinject the canonical `restic.env` before dispatching the restore script.
- Chain the existing update phases after a successful restore when requested; an update failure downgrades to a completion warning.
- Stream the restore exec's output line-by-line into the operation log (ConcurrencyGroup's `on_line`), throttled, instead of only coarse desktop-side phase lines.
- Store the operation log on the registry record (size-capped) and replay history when a log stream attaches; keep the SSE route contract otherwise unchanged.
- Add a CANCELLED terminal status to `WorkspaceOperationStatus`; cancelled operations end as CANCELLED (not FAILED) with no error styling.

### API (`api_v1.py`, `api_models.py`, `api_schema.py`)

- Restore route body grows `update_after` (default true), `skip_safety_snapshot`, and `skip_chat_gate` flags alongside `stop_chats`.
- Strict `limit`/`offset` parsing: 400 on non-integer or negative values.
- Operation status response reflects CANCELLED and carries whatever the UI needs to render the neutral notice and the completion warning.

### UI (`backup_operation_ui.js`, `backup_table.js`, `RestoreDialog.jinja`, `BackupOperationStrip.jinja`)

- "Update the backup service afterwards" checkbox in the restore dialog, default checked.
- Toggleable details panel in the operation strip showing the streamed log for all backup operations; full history on attach.
- "Restore without backing up first" and "Force restore" retry buttons in the failure notice, shown only for the matching failure; both dispatch immediately.
- CANCELLED renders as a neutral notice; completion-with-warning renders success plus the warning text.

### default-workspace-template (separate PR, same branch name, via `just dwt-worktree`)

- Pin restic 0.18.1 (sha256-verified download) in the image so new workspaces never need the in-script download.

### CI and tests

- Fix `_download_backup_zip` in `test_sync_e2e.py`: navigate once, poll the selector without reloading.
- Add a `workflow_dispatch` test-filter input to ci.yml's snapshot job (passed through to `just test-offload-minds-snapshot`'s existing `--filter`); use it to verify the e2e fix on this branch (`dev/` changelog entry).
- New snapshot-resume restore e2e in `test_snapshot_resume.py` style: baked workspace, local restic repo via `configure_backups_for_host` (API_KEY provider), real restore worker path, sentinel rewind + `restored` tag + services RUNNING assertions. Also cover the failure affordances cheaply where possible (e.g. force-restore flag honored).
- Bump the snapshot offload `max_parallel` (and its comment) if the `minds_snapshot_resume` test count approaches it, so tests never share an unclean sandbox.
- Rework the script-level restore tests for the new in-place flow (sync restore, version-check/download stub, unlock retry, excludes from backup.toml, skip flags, streamed output); update worker/API/UI tests for the new flags, CANCELLED, log replay, and strict parsing.
- Changelog entries: `apps/minds` (restore rework + UX) and `dev` (ci.yml filter input); dwt PR carries its own.

### Style cleanups (fold into the commits touching each file)

- Remove the newly added default arguments (`start_if_idle` target, `_dispatch_backup_worker` operation_target); `Mapping` instead of `dict` for the worker-kwargs input; dedupe `_stub_bin_with_restic`'s `agents_json` branching; eliminate the `ws_name` reassignment in the new page handler.
