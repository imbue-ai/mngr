Tiered system-interface restart for the minds recovery flow.

- When a workspace's system interface stops responding, minds shows a
  recovery page. While it is checking host health or a restart is in
  flight it shows a single "Loading workspace" state and refreshes itself
  until the workspace is back.
- The recovery page picks its tier from the workspace host's state and
  recovers with no clicks where it safely can. A running container gets a
  surgical system-interface restart (which does not interrupt your
  agents); a fully stopped container gets a full restart immediately
  (nothing is running, so there is nothing to interrupt). Only an
  ambiguous host state falls back to a confirmed "Restart workspace"
  button.
- If a restart fails, the page shows the workspace as unresponsive and
  offers a full workspace restart, with the error detail collapsed by
  default (expandable, and it wraps instead of overflowing its
  container). All of the recovery page's non-loading states share one
  "Workspace unresponsive" heading; the post-failure state in particular
  no longer says "Restart failed" -- the automatic restart runs
  invisibly behind the "Loading workspace" state, so naming a failed
  attempt the user never saw was just confusing.
- The surgical restart cleanly stops and starts the system-services
  agent instead of poking its tmux window; the full restart bounces the
  whole workspace container.
- The recovery page's loading state is visually consistent with the
  forwarding plugin's "Loading workspace" loader, so the two pages a user
  may see during recovery look like one page.
- The sidebar workspace context menu gains a "Restart workspace…" entry
  (with a confirmation, since it interrupts every agent), and the home
  page gains a per-workspace restart button.
- Opening a workspace whose container has been stopped now routes to the
  recovery page (and serves the styled "Loading workspace" loader)
  instead of flashing a raw error.
- The recovery page's "Loading workspace" state no longer shows the
  explanatory "This page will reload automatically..." line -- it just
  shows the heading.
- The recovery page now auto-refreshes on a 1s cadence rather than
  1.5s, so its self-reload coincides with a completed rotation of the
  loading spinner instead of jumping the spinner back mid-rotation.
- The recovery page no longer flashes up for a workspace that is actually
  healthy. A workspace is now only treated as stuck after the background
  probe loop confirms it unreachable with a sustained run of failed HTTP
  probes; a single transient backend hiccup (such as a recycled SSE
  stream) merely starts active probing instead of triggering recovery.
- Minds' HTTP calls through the forwarding plugin -- the
  workspace-readiness / health probes and the refresh-service broadcast
  POST -- now connect to the plugin over loopback and carry the agent's
  ``agent-<hex>.localhost`` vhost in the ``Host`` header, instead of
  putting the subdomain in the request URL. The plugin already routes on
  the ``Host`` header, so this makes those calls independent of
  ``*.localhost`` name resolution, which is not available on every host.
