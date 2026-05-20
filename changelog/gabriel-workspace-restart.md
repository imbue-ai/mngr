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
  page gains a per-workspace restart button.
- New `mngr stop --stop-host` flag: stops an agent's whole host instead
  of just the agent.
