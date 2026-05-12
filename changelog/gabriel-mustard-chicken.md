When a workspace server becomes unresponsive (HEALTHY -> STUCK transition),
the chrome titlebar now auto-navigates the content view to the recovery page
for the affected agent instead of attempting to render an in-titlebar banner.
The recovery page already redirects back to the original workspace URL once
the server is healthy again. The in-titlebar banner approach didn't work in
Electron because the banner was positioned outside the chrome WebContentsView's
bounds, where the content view covered it.

Workspace-server restart UX improvements:

- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- Clicking "Restart workspace server" on the recovery page now fires the
  restart and immediately navigates back to the workspace URL, where the
  plugin's loader handles the wait. The recovery page no longer blocks on
  the restart API.
- The sidebar right-click "Restart workspace server" menu item now triggers
  the restart and navigates straight to the loading page, skipping the
  intermediate recovery page click.
- Fixed a bug where `workspaceUrlForAgent` in Electron's main process built
  `/goto/<agent>/` URLs against the minds port instead of the mngr_forward
  plugin port (which owns `/goto/`). This caused 404s after a manual restart
  from the sidebar context menu and from "Open in new window".
