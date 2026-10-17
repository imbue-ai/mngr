Workspace content URLs are now keyed by host id and services own their origins.

Local workspace origins become `[<service>.]host-<hex>.localhost:8421`: the bare origin serves the shell and each registered service is reached at its own `<service>.` origin (no more `/service/<name>/` path prefix). The `/goto/` bridge, sidebar/landing/inspiration links, the sharing editor's workspace link, and the create-flow redirect all navigate by host id.

The Electron surface routing, window dedupe, accent tinting, and recovery redirect understand both workspace coordinates: content URLs carry host ids while minds records and SSE events stay agent-keyed, with alias maps (fed by the workspaces payload, which now carries `host_id`) translating between them. The recovery page and `/help` accept either coordinate. The `restorable_workspace_ids` restore-filter payload carries both coordinates too, so host-keyed persisted windows survive an app restart, and window entries persisted by older agent-keyed builds are migrated to the host coordinate on restore.

Workspace health probes (create-flow readiness, background health loop, restart recovery) send the `host-<hex>.localhost` vhost in the Host header.
