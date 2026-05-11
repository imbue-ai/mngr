Re-implement workspace-server restart and health-recovery UI on top of the new `mngr_forward` plugin architecture.

User-visible changes:
- A persistent banner now appears at the top of the workspace iframe when the agent's workspace server stops responding. Clicking it opens the recovery page so the user can restart the server.
- The landing page now annotates each project row with a status badge when its workspace server is unresponsive or restarting; clicking such a row goes to the recovery page instead of the workspace.
- The sidebar context menu gained a "Restart workspace server" entry that opens the recovery page for the selected workspace.
- A dedicated recovery page (`/agents/<id>/recovery`) renders the restart button, streams server-status updates via SSE, and auto-reloads back to the workspace once the server is healthy again.
- The plugin emits `workspace_backend_failure` envelopes when it sees connection errors, mid-SSE EOF, or 5xx responses from the workspace backend. Minds tracks these as a per-agent state machine (HEALTHY -> STUCK after 5 seconds of continuous failures -> RESTARTING during a user-triggered restart -> back to HEALTHY on the first successful probe).
- When the plugin returns 503 for an HTML navigation and a minds origin is configured, it now 302-redirects the browser to the recovery page rather than serving the legacy auto-refresh page.
