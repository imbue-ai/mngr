Added the `WorkspaceId` typed wrapper (the workspace's `system-services` agent id -- no new id space).

Sync records push through the workspace-keyed connector routes (`/sync/records/by-workspace/...`) with a host-route fallback for older connectors; `sync records delete` accepts a workspace id or a host id, and its JSON output now reports the deleted id under `record_id` (previously `host_id`). The record wire model gains the optional `backup_bucket` field.

`shares create` gains `--workspace-id`, keying the share by the workspace (the connector mints and persists a share label so the domain carries no internal id and survives machine changes).
