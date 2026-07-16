Adds a read-only backup history UI to the workspace settings page and a dedicated full-history page, plus a per-snapshot Download action.

New "Recent backups" table on the settings page: newest snapshots with relative time and a per-row Download.

New "View all N backups" footer linking to a dedicated full backup-history page (its own route, template, and JS) with client-side Newer/Older paging.

Both surfaces load snapshots from the existing `GET /api/v1/workspaces/<id>/backups` response, which now returns them newest-first so neither surface has to re-sort; the history page pages that list in the browser and there is no separate paged snapshots API.

Listing snapshots and exporting them run restic on this machine, so viewing history and downloading a backup work even when the workspace is offline.

Shared row builder (`backup_table.js`) used by both tables so they cannot drift; Download only.

Removes the stray "download" link from the Landing page now that download lives in the table.
