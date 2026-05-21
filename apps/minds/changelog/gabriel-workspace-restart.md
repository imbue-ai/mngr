Tiered system-interface restart for the minds recovery flow.

- The workspace recovery page now has two restart tiers. On load it runs
  a host-health check and shows "Checking host health" until it resolves.
  When the container is reachable it immediately runs a surgical
  system-interface restart (which does not interrupt your agents), so the
  workspace recovers with no clicks; when the container is unreachable it
  offers a full "Restart workspace" button instead.
- If the surgical restart does not bring the workspace back, the page
  offers the full workspace restart as an escalation. If that also
  fails, it shows the failure reason with a try-again option.
- The surgical restart now cleanly stops and starts the system-services
  agent instead of poking its tmux window; the full restart bounces the
  whole workspace container.
- The sidebar workspace context menu gains a "Restart workspace…" entry
  (with a confirmation, since it interrupts every agent), and the home
  page gains a per-workspace restart button. The sidebar restart entries
  now show restart progress on the recovery page instead of immediately
  reloading the (still-running) workspace, and the home-page restart
  button opens the recovery page's tier picker even when the workspace is
  currently healthy.
- Opening a workspace whose container has been stopped now routes to the
  recovery page (and serves the styled loader) instead of flashing a raw
  error.
- The recovery page's host-health probe no longer starts the workspace
  container as a side effect, so a genuinely stopped workspace is
  reported as unreachable and offered the full workspace restart, instead
  of being silently started by the probe and offered only the surgical
  restart.
