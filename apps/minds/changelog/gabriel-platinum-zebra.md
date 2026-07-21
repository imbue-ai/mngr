Workspace resources (CPU + memory) can now be resized for local docker and lima workspaces:

- The per-workspace settings page gains a Resources section (hidden for providers without resize support) showing the configured CPU/memory with the machine's available ceiling. Saving persists immediately and never restarts by itself; when the change could not apply live, a post-save dialog offers "Restart now" (reusing the existing host restart operation, with progress) or "Apply on next restart", and a standing note marks the pending values. A reset button restores the provider defaults. Typing values above the machine's capacity shows a non-blocking over-provisioning warning.

- The create page's advanced view gains CPU and memory fields for docker/lima workspaces, so a workspace can be born with the requested allotment (passed through as provider start args).

- New API endpoints: `GET /api/v1/workspaces/<id>/resources` (capabilities + configured + actual values; rides the existing `minds-workspaces-read` grant) and the set-only `POST /api/v1/workspaces/<id>/resize`, exposed to agents through the latchkey gateway behind a new target-scoped `minds-workspaces-resize` permission -- an agent hitting memory pressure can request more for its own workspace, while restart power stays behind the separate `minds-workspaces-recover` grant.
