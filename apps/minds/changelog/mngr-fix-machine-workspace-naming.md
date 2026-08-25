Machine/workspace identity cleanup (see `specs/machine-workspace-naming/`): a workspace is identified by its `system-services` agent id (its workspace id); the machine it runs on (host id) is a swappable attribute.

The synced workspace-record replica is keyed by workspace id (host_id becomes the record's mutable current-machine attribute); the backup reaper, quota eviction, and remove-from-list address records by workspace id, dual-accepting legacy host ids.

New backup buckets are named by workspace id (existing host-named buckets are grandfathered), and the record carries an explicit `backup_bucket` for the server-side reaper.

Sharing passes the workspace id to the connector so new shares get minted, persisted share labels (no internal id in the public domain); the sharing API moves to `/api/v1/workspace-sharing/<workspace_id>` (the `/machines/<host_id>/sharing` routes remain as compat shims).

Workspace content URLs (`/goto/<id>/` and the `.localhost` origin family) are keyed by the workspace id, so they survive machine changes; legacy host-keyed URLs redirect. The AI-key mint page keys on the workspace id and dual-accepts host ids from older in-workspace deep links.

Renames: `WorkspacePaths` -> `InstallationPaths`, the `workspace-id` host label -> `create-attempt-id` (legacy label still read), the install device id gets its own `DeviceId` type, and the create form's name-validation error says "Workspace name".
