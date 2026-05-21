Tiered system-interface restart for the minds recovery flow.

- The workspace recovery page now has two restart tiers. On load it runs
  a host-health check and shows "Checking host health" until it resolves;
  it then offers a surgical "Restart system interface" (which does not
  interrupt your agents) when the container is reachable, or goes
  straight to a full "Restart workspace" when it is not.
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
- New `mngr stop --stop-host` flag: stops an agent's whole host instead
  of just the agent.
- Opening a workspace whose container has been stopped now routes to the
  recovery page (and serves the styled loader) instead of flashing a raw
  error: the forwarding plugin reports both an SSH-tunnel setup failure and
  a refused host-loopback dial as backend failures, the same as other
  unreachable-backend cases.
- The recovery page's host-health probe no longer starts the workspace
  container as a side effect. It now runs `mngr exec --no-start`, so a
  genuinely stopped workspace is reported as unreachable and offered the
  full workspace restart, instead of being silently started by the probe
  and offered only the surgical restart.
