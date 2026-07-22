Merged the contributed workspace backups stack (shiyanboxer/mngr Parts 2-5) into the main repo:

- Relabeled and reorganized the workspace Settings "Backups" section in plain language (Recent backups table, "Where your backups are stored", "Fix backup problems").

- Added a read-only backup history UI: a "Recent backups" table on Settings (newest five, per-snapshot Download) and a full backup-history page with server-side paging.

- The workspace backups API (`GET /api/v1/workspaces/<id>/backups`) now accepts `limit`/`offset` and returns `snapshots_total`; snapshots are returned newest-first.

- Added in-place restore: a per-row "Restore" action (confirmation dialog) runs as a tracked `BACKUP_RESTORE` operation via `POST /api/v1/workspaces/<id>/backups/<snapshot_id>/restore` and a self-contained workspace script that takes a pre-restore safety snapshot, restores into staging, swaps it in (preserving `restic.env`), reinstalls dependencies, and restarts services. A running restore reports on its own table row with a Cancel that is withdrawn once mutation starts.

See the individual `shiyan-backup-*.md` entries in this directory for full detail.
