When a workspace server stops responding, the chrome auto-navigates the
content view to a recovery page for the affected agent. The recovery page
streams server-status updates over SSE and reloads back to the workspace
once the server is healthy again.

Workspace-server restart UX improvements:

- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- Clicking "Restart workspace server" on the recovery page now fires the
  restart and immediately navigates back to the workspace URL, where the
  plugin's loader handles the wait. The recovery page no longer blocks on
  the restart API.
- The recovery page now notes that running agents are not interrupted by a
  workspace-server restart.
- The sidebar right-click "Restart workspace server" menu item now triggers
  the restart and navigates straight to the loading page, skipping the
  intermediate recovery page click.
- Fixed a bug where `workspaceUrlForAgent` in Electron's main process built
  `/goto/<agent>/` URLs against the minds port instead of the mngr_forward
  plugin port (which owns `/goto/`). This caused 404s after a manual restart
  from the sidebar context menu and from "Open in new window".
- Stale failure envelopes arriving immediately after a successful restart
  no longer cause a brief recovery-page flash; the health tracker now
  ignores failures within a short grace window after recovery.
