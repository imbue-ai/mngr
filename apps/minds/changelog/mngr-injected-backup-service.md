The minds backup service can now be verified and idempotently updated on running workspaces:

- The backup status check is expanded with a per-workspace backup-service verification: one exec per online workspace compares the installed `libs/host_backup` code against the `minds-v*` tag matching the app version ("newer than the tag" is fine and never flagged), checks the `host-backup` supervisord program is RUNNING, and compares the workspace `restic.env` against the minds-side canonical copy. Any problem (including backups not being configured at all) shows a single warning badge on the workspace tile; a new batch `GET /api/v1/workspaces/backup-health` route feeds it.

- A one-click idempotent "Update backup service" action (workspace settings) converges everything: the code is checked out at the target tag and committed as `backup-update: minds-v<X>` (stashing and restoring uncommitted work), `uv sync` runs, and the service is restarted and verified, with automatic `git revert` rollback on failure. Actively-RUNNING chats block the code path with a "Stop all chats and retry" follow-up; the update waits (cancellably) for any in-flight backup tick. Runs as a tracked workspace operation with live progress.

- Backups can now be enabled on a configure-later workspace post-creation, and a workspace's backup destination can be changed (fresh provisioning against the new repository; the old canonical env is archived and existing snapshots stay reachable). Env re-injection rotates a drifted workspace `restic.env` aside to `restic.env.<timestamp>` instead of overwriting it.

- A workspace with a working, externally-configured `restic.env` and no minds-side canonical copy is adopted automatically during the check, so status and management just start working.

- A per-workspace "backup verification" toggle disables the checks and badge entirely for workspaces that deliberately run without backups.
