When a workspace server stops responding, the chrome auto-navigates the
content view to a recovery page for the affected agent. The recovery page
streams server-status updates over SSE and reloads back to the workspace
once the server is healthy again.

Workspace-server restart UX improvements:

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
