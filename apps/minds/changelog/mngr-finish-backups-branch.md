Merged the contributed workspace backups stack (shiyanboxer/mngr Parts 2-5) and finished it with a substantial rework of the restore path:

- Relabeled and reorganized the workspace Settings "Backups" section in plain language (Recent backups table, "Where your backups are stored", "Fix backup problems"), added a read-only backup history UI (Settings recent table + a full backup-history page with server-side paging), and added `limit`/`offset` + `snapshots_total` to `GET /api/v1/workspaces/<id>/backups` (snapshots newest-first; malformed or negative `limit`/`offset` now 400 instead of silently meaning "all").

- In-place restore (`POST /api/v1/workspaces/<id>/backups/<snapshot_id>/restore`) now restores as a sync: `restic restore <snapshot>:<subpath> --target /mngr --delete --overwrite if-changed` -- no staging copy (no double disk), only changed files rewritten, and a failed restore converges when simply re-run. minds resolves the snapshot's host-dir subpath from its own view of the repository before dispatch.

- The restore works fleet-wide: when the workspace's restic predates `restore --delete` (Debian bookworm ships 0.14), the script downloads the pinned sha256-verified restic 0.18.1 (amd64/arm64) and installs it persistently. The canonical `restic.env` is reinjected before dispatch so workspaces with lost or drifted credentials can still restore, stale repository locks are cleared with `restic unlock` + one retry, the safety snapshot honors the user's current `backup.toml` excludes, and the tick-in-flight check is self-healing (a stopped host-backup service cannot have a live tick).

- The restore dialog gained an "Update the backup service afterwards" checkbox (default on) that chains the idempotent update converge onto a successful restore; a chained-update failure downgrades to a completion warning instead of failing the restore. The failure notice offers targeted retries: "Stop chats and try again", "Restore without backing up first" (after the safety snapshot failed), and "Force restore" (when the workspace can no longer answer the chat gate).

- All tracked backup operations stream their full output live into a collapsible details panel (operation logs are stored server-side and replayed to any page attaching mid-operation), a running restore reports on its own table row with a Cancel withdrawn at the point of no return, and a user cancel now ends the operation in a neutral CANCELLED state instead of a red error.

See the individual `shiyan-backup-*.md` entries in this directory for the contributed base.
