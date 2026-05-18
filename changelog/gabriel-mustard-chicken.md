Workspace-server restart and health-recovery UI on the `mngr_forward` plugin architecture.

User-visible changes:

- When an agent's workspace server stops responding, the chrome auto-navigates the workspace view to a recovery page where the user can restart the server. The recovery page streams server-status updates over SSE and reloads back to the workspace once the server is healthy again.
- The landing page now annotates each project row with a status badge when its workspace server is unresponsive or restarting; clicking such a row goes to the recovery page instead of the workspace.
- The sidebar context menu gained a "Restart workspace server" entry that opens the recovery page for the selected workspace.
- A dedicated recovery page (`/agents/<id>/recovery`) renders the restart button, streams server-status updates via SSE, and auto-reloads back to the workspace once the server is healthy again.
- The plugin emits `workspace_backend_failure` envelopes when it sees connection errors, mid-SSE EOF, or 5xx responses from the workspace backend. Minds tracks these as a per-agent state machine (HEALTHY -> STUCK after 5 seconds of continuous failures -> RESTARTING during a user-triggered restart -> back to HEALTHY on the first successful probe).

Restart UX improvements on top of the above:

- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- The `/api/agents/<id>/restart-workspace-server` endpoint now returns 200
  as soon as the `mngr exec` kill dispatch completes (it no longer blocks
  for up to 15 seconds polling the workspace through the plugin). The
  background workspace-health probe loop continues to flip the tracker back
  to HEALTHY once the workspace is responsive. This makes the endpoint a
  reliable "the workspace has been killed" signal for callers that want to
  navigate to the plugin's loader page.
- The recovery page's "Restart workspace server" button and the sidebar
  right-click "Restart workspace server" menu item now both await the
  restart API response before navigating to the workspace URL. Previously
  they fired the POST and navigated immediately, which on a still-healthy
  workspace raced against the in-flight kill and silently reloaded onto
  the unchanged iframe. Awaiting guarantees the user lands on the plugin's
  "Workspace server starting..." loader.
- The recovery page now notes that running agents are not interrupted by a
  workspace-server restart.
- Stale failure envelopes arriving immediately after a successful restart
  no longer cause a brief recovery-page flash; the health tracker now
  ignores failures within a short grace window after recovery.
