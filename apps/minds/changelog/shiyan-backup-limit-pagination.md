The workspace backups API (`GET /api/v1/workspaces/<id>/backups`) now accepts optional `limit` and `offset` query params and returns a new `snapshots_total` count alongside the snapshot window. `limit` absent still returns every snapshot (unchanged behavior); `limit=0` returns none, and both must be non-negative integers.

The Settings "Backups" surfaces use this to only fetch the snapshots they render instead of the full list: the "Recent backups" table requests just the newest five (driving "View all N backups" off `snapshots_total`), and the full backup-history page pages server-side with `limit`/`offset` rather than loading every snapshot and slicing in the browser. The workspace-list badge surfaces (sidebar/chrome and the Landing page) request `limit=0` and `limit=1` respectively, since they read only verification status or the single newest snapshot.

This is a payload optimization only: no change to how backups are created, listed, or restored.
