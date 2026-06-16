# Tiered system-interface restart (v2)

## Overview

- Replace the precise "kill the `svc-system_interface` tmux window + `touch services.toml`" surgical restart with a coarser, cleaner one: `mngr stop system-services` then `mngr start system-services`. This restarts the whole `system-services` agent (including the bootstrap manager) instead of depending on bootstrap's file-watch loop already running.
- Add a second, heavier recovery tier: a full host restart, `mngr stop system-services --stop-host` followed by `mngr start system-services`. This bounces the agent's container (not the VM) and interrupts every agent in the workspace.
- Extend the `mngr stop` CLI with a `--stop-host` flag that stops the agent's host via the existing `ProviderInstance.stop_host` API. For every provider minds uses (local `docker`, `imbue_cloud`) and for `vps_docker`/Vultr, `stop_host` stops only the Docker container; the underlying VM/VPS keeps running. The flag fails fast on providers whose `supports_shutdown_hosts` is `False`.
- Make the recovery page a two-tier flow: it probes once on load to decide whether the surgical restart is worth offering, escalates to the host restart if surgical does not recover the workspace within 15 seconds, and shows a terminal error state if the host restart also fails.
- Keep the existing recovery infrastructure unchanged in shape: STUCK detection via `workspace_backend_failure` envelopes, the chrome auto-redirect to the recovery page, the background health-probe loop, and the `system_interface_status` SSE channel.

## Expected behavior

- When the system interface wedges, the agent still goes STUCK after continuous failures and the chrome still auto-redirects the content view to the recovery page.
- After the recovery page loads, it runs one lightweight layer-2 probe that checks only whether the container is reachable and able to run `mngr` agent operations (it does not check the `system-services` session or `ttyd`, since the surgical restart recreates those anyway).
  - While the probe is in flight, the page shows a "Checking host health" state with no restart button — the surgical button is not offered until the probe outcome is known.
  - Probe succeeds: the page replaces "Checking host health" with a "Restart system interface" button (the surgical tier).
  - Probe fails: the page hides the surgical button entirely and offers only the full host restart.
- Clicking "Restart system interface" runs `mngr stop system-services` + `mngr start system-services`, restarting only the `system-services` agent; the user's claude agent is left untouched.
  - Recovery is confirmed when `system_interface` answers HTTP 200 through the plugin (the existing `probe_workspace_through_plugin` check). On recovery the page navigates straight back to the workspace, no confirmation.
  - If `system_interface` does not respond within 15 seconds, or the `mngr` commands themselves error out, the page reveals a second button for the full host restart.
- The full host restart button is labelled to warn that all agents in the workspace will be interrupted. Clicking it (no extra confirmation dialog) runs `mngr stop system-services --stop-host` + `mngr start system-services`.
  - This stops the whole container, then starts the host and only the `system-services` agent. The user's claude agent is intentionally left stopped; it will be started template-side when the user sends their next message.
  - Recovery is confirmed the same way (system_interface HTTP 200). On success the page navigates straight back to the workspace.
  - If the host restart also fails to recover within 15 seconds, the page shows a terminal error state with the failure reason and a try-again / contact-support message.
- `mngr stop <agent> --stop-host` from the CLI stops the entire host the agent runs on (all agents on it go down). On a provider that does not support stopping hosts it fails immediately with a clear error.
- The 15-second wait for `system-services` to come back is a single shared constant used by every recovery wait point (surgical and host-restart). Initial agent-creation readiness waiting keeps its own, separate timeout.
- Manual recovery affordances stay available outside the recovery page: the sidebar workspace context menu keeps its "Restart system interface" entry (now surgical via `mngr` stop/start) and gains a "Restart workspace" entry for the host restart; the home/landing page gains an equivalent restart affordance per workspace row.

## Changes

- `mngr stop` CLI: add a `--stop-host` flag. With it, the command stops the agent's host through the existing `stop_host` provider API (no snapshot) instead of just the agent's tmux session. Validate up front that the provider supports stopping hosts and error clearly if not. Update the command's help text and examples.
- The system-interface restart endpoint in minds: drop the `tmux kill-window` + `touch services.toml` mechanism. The surgical restart now dispatches `mngr stop` + `mngr start` against the `system-services` agent that shares the workspace's host (scoped to that host so it never matches other workspaces' `system-services` agents).
- Add a host-restart path in minds that dispatches `mngr stop system-services --stop-host` + `mngr start system-services`. Both restart paths return promptly and drive completion asynchronously, marking the health tracker RESTARTING while in flight.
- Add a layer-2 reachability probe in minds: a lightweight check that the workspace's container/host is reachable via `mngr`, exposed so the recovery page can call it asynchronously after load. The page renders a "Checking host health" interim state and updates to the surgical or host-restart tier once the probe resolves.
- System-interface health tracker: add a terminal failure state (carrying the failure reason) for when a restart tier fails to recover the workspace within the 15-second window or its `mngr` commands error. The `system_interface_status` SSE payload carries this state and reason so the recovery page can render the escalation button or the terminal error.
- Recovery page (`recovery.html`): rework into the two-tier flow described above — initial tier selection from the layer-2 probe, surgical-then-escalate sequencing, the host-restart warning copy, the terminal error state, and navigation straight back to the workspace on recovery.
- Introduce a single shared "system interface startup wait" constant (15s) and use it for every recovery wait point; leave initial agent-creation readiness waiting on its own timeout.
- Electron sidebar context menu (`main.js`): add a "Restart workspace" entry that triggers the host restart, alongside the existing "Restart system interface" entry.
- Home/landing page: add a per-workspace restart affordance consistent with the recovery page's tiers.
- Add a changelog entry describing the new tiered restart and the `mngr stop --stop-host` flag.
- Tests: cover the `--stop-host` flag (including the unsupported-provider error), the surgical and host-restart dispatch paths, the layer-2 probe and tier selection, the escalation-on-timeout and terminal-error transitions, and the recovery page rendering for each state.
