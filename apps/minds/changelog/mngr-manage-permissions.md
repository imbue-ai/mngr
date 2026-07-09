The app-level Settings page is now organized into a left nav (Permissions, Error reporting) with a right content pane, replacing the single-column layout.

The app-level Settings page gains three new permission sections in its left nav, letting you inspect and revoke -- across all active workspaces -- the access your agents have been granted:

"Connectors": third-party services your agents have connected to (Slack, GitHub, ...). Each service lists one card per workspace that has access, with the granted permissions; hover a permission to see what it allows. Revoke a single workspace's access, or use "Revoke all" to revoke that service from every workspace at once. Your saved sign-in is kept, so agents can reconnect later.

"File sharing": local files and folders your agents can access over the shared file mount. Each workspace card shows its access level -- "read" and/or "read and write" -- and hovering it lists the actual shared paths (one per line). Revoke a single workspace's file sharing or all of it at once.

"Workspace delegation": access you've granted agents in one workspace to manage other workspaces (list/create plus targeted operations like destroy, start/stop, SSH, and health checks), grouped by the workspace being managed. Each card is a workspace that holds the access, and hovering a verb explains what it allows. Revoke per workspace within a group, or a whole group at once.

Revocation only removes the relevant rules and leaves unrelated permissions intact.

Revoking removes only the permission rule (through the latchkey gateway's permissions extension); your saved sign-in for the service is left in place, and agents can request access again later through the usual permission-request flow. Changing or broadening an existing grant is still done via that request flow, not from this page. Destroyed workspaces are not shown.
