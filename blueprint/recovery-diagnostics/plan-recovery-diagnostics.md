# Recovery diagnostics

Add richer health/diagnostic probing to the workspace recovery flow on `gabriel/workspace-restart`. Introduces a third "misconfigured / no restart will help" tier alongside surgical and host-restart, surfaces a debug menu on the recovery page, and adds an SSH-dead auto-escalate path — without flooding the container with probes.

## Overview

- The existing recovery flow correctly drives the tier choice between surgical and host-restart from `agent.host.state` via `mngr list`, but offers no signal about *why* a restart failed or whether a restart can ever help. This plan adds a small set of targeted probes to fill that gap without adding ongoing probe traffic during normal operation.
- The only new tier-gating signal is **Q4 (services.toml declares `[services.system_interface]`)**. A missing declaration is the only condition where we are confident no restart can possibly help; everything else (in-container probe failures, missing plugin resolver entry) remains transient enough that a restart attempt is still warranted.
- The SSH-dead-but-host-RUNNING edge case is detected from the batched probe failing to produce its sentinel output and is steered to the host tier — but does **not** auto-dispatch. The existing auto-dispatch invariant is "no in-flight user work is interrupted" (surgical doesn't touch user agents; host-restart of an already-stopped host has nothing to interrupt). A host restart with the host RUNNING and SSH-dead would bounce a live container with running user agents, so we treat it like today's "ambiguous host state" path and require a confirming click.
- Plugin-side resolver state is exposed to minds via a new `ResolverSnapshotPayload` envelope on the existing plugin stdout stream, mirroring `ListeningPayload` / `LoginUrlPayload` and avoiding a new file or HTTP route. Plugin emits on every resolver mutation; minds keeps the latest copy in process state.
- Probes run only on recovery-page load when the tracker is not RESTARTING — one batched `mngr exec` + one `mngr list` + one in-memory snapshot read. RESTARTING refreshes skip probing entirely. Normal healthy operation is untouched.

## Expected behavior

- **Healthy workspace, unchanged paths.** The home page, sidebar, chrome SSE, layer-1 background probe loop, agent creation, and destruction flows all behave as today. No new probe traffic.
- **Workspace becomes STUCK with services.toml correctly declared.** The recovery page loads, runs the batched probe + `mngr list` + reads the resolver snapshot. Host RUNNING → surgical restart auto-dispatches (as today). Host STOPPED/CRASHED → host restart auto-dispatches (as today). Either way the debug `<details>` menu carries the probe results, but is closed by default and the user does not see them unless they expand.
- **Workspace becomes STUCK with services.toml *missing* `[services.system_interface]`.** Recovery page renders the misconfigured variant: rewritten heading and body copy explaining the workspace is misconfigured and a restart is unlikely to help. No auto-dispatch. A small structured checklist (one item per probe: host RUNNING / SSH reachable / system-services RUNNING / services.toml declares / in-container probe / plugin resolver entry) renders with pass / fail / warn icons. A "Copy diagnostics" button copies the raw text. A secondary "Try restart anyway" button dispatches a host restart.
- **Host RUNNING but SSH transport down.** The batched `mngr exec` script's `===PROBE-READY===` sentinel never reaches minds. The recovery page does *not* auto-dispatch — bouncing a live container with running user agents requires consent, same as today's ambiguous-host-state path. The page renders the shared "Workspace unresponsive" copy with the structured checklist visible (so the user can see SSH is the failing item), and the primary button is a host restart (since surgical would fail at the `mngr stop` step that needs SSH). One click → host restart proceeds, "Loading workspace" loader, normal recovery sequence.
- **Restart attempt finishes and the workspace is still STUCK.** The next render re-runs the batched probe (the tracker is no longer RESTARTING). The debug menu now carries the *post-restart* observations, useful for explaining why the attempt did not recover the workspace. The misconfigured tier can also be entered post-restart if the restart attempt clobbered services.toml.
- **Restart attempt finishes and the workspace is HEALTHY again.** Recovery page redirects to `return_to` as today. The final probe-result set is logged at INFO via loguru on the STUCK→HEALTHY transition. No UI history.
- **Debug `<details>` menu on every recovery render.** Always populated on the page-load batch, closed by default. Contents: Q1-agent state from `mngr list`, Q3 `tmux ls` output, Q4 services.toml status, Q5 process bound to the inner port, Q6 in-container probe result, Q7 plugin resolver mapping for this agent. Below the checklist: copyable SSH connection string(s) for the host (one line per host, with the user@host / port / key path), derived from `mngr list --format json`. The plugin's outer `mngr_forward_port` is a constant the page already has and is included alongside.
- **Plugin restart with a still-running minds.** When `mngr forward` restarts, its in-memory resolver starts empty and rebuilds as agents republish their `services` envelopes. The plugin emits a `resolver_snapshot` envelope on each mutation; minds' in-memory copy ends up consistent. While the rebuild is in progress, recovery pages render Q7 as "no entry yet" for agents the plugin hasn't seen yet — honest signal, no special handling.
- **Older minds running against newer plugin (and vice-versa).** Unknown envelope payload types are already TRACE-logged and dropped by `forward_cli.py`'s `_handle_forward_payload`; old minds against new plugin just doesn't have Q7, no failure mode. New minds against old plugin sees no `resolver_snapshot` envelopes and renders Q7 as "no entry yet" — same as the post-plugin-restart transient.

## Changes

- **`libs/mngr_forward`**
  - Add a `ResolverSnapshotPayload` envelope type alongside the existing `ListeningPayload` / `LoginUrlPayload` / `SystemInterfaceBackendFailurePayload` / `ReverseTunnelEstablishedPayload`. Discriminated by `type: "resolver_snapshot"`, carrying the full per-agent resolver map.
  - Have the `ForwardResolver.update_services` mutation path emit the envelope on each change, via the existing envelope writer used by the rest of the plugin's stdout stream. No periodic flushes, no debouncing, no initial empty emission at startup — first emission happens when the first real services envelope is consumed.
  - Update the envelope schema docs and tests to cover the new payload type.
- **`apps/minds/imbue/minds/desktop_client/forward_cli.py`**
  - Extend `EnvelopeStreamConsumer` with `_handle_resolver_snapshot` to record the latest snapshot in a new private field, and a public accessor that returns the current per-agent map (or an empty dict if no snapshot has arrived yet).
  - Wire the new `_handle_resolver_snapshot` into `_handle_forward_payload`'s type dispatch.
- **`apps/minds/imbue/minds/desktop_client/app.py`**
  - Extend the batched probe script run via `mngr exec`. The script starts with `echo "===PROBE-READY==="`, then prints a small block per probe (Q3 `tmux ls`, Q4 TOML parse of `/code/services.toml`, Q5 `ss -ltnp` filtered to the inner port, Q6 `curl -m1 http://localhost:<port>/`). Inline Python (`python3 -c "import tomllib..."`) handles the Q4 check authoritatively.
  - Extend the host-health probe endpoint to run the batched `mngr exec` (host-side), parse its output into a typed record, and return the full record — alongside the existing `reachable` / `host_offline` fields — for the recovery-page client to consume. Endpoint discriminates between "probe ran" (sentinel present), "SSH dead" (sentinel absent — adds `ssh_dead: true`), and "command framework error".
  - Add the `is_misconfigured` field, set when the parsed probe says services.toml does not declare `[services.system_interface]`. Treat this as a fourth top-level classification alongside `reachable` / `host_offline` / ambiguous.
  - Provide a way to read the system-services agent's lifecycle state from the same `mngr list --format json` we already call (already in the JSON; needs only a small extraction). Pass through to the host-health endpoint response.
  - Surface SSH connection string(s) derived from `mngr list --format json`'s `host.ssh.*` fields on the same endpoint response.
  - Read the latest resolver snapshot from `EnvelopeStreamConsumer` and include the relevant agent's services map in the host-health endpoint response.
  - Bound the batched `mngr exec` to a 5s hard ceiling; surface timeout as a transport-class failure (treat as SSH dead → host-restart auto-escalate path).
  - On STUCK→HEALTHY tracker transition, log the final probe results at INFO via loguru.
  - The `_dispatch_restart` / `_handle_restart_host_api` paths require no functional change — the misconfigured page's "Try restart anyway" reuses `_handle_restart_host_api`.
- **`apps/minds/imbue/minds/desktop_client/templates.py`**
  - Extend `render_recovery_page` to accept the parsed probe result and the resolver snapshot.
  - Add a new `misconfigured` initial-state branch alongside the existing `stuck` / `restarting` / `restart_failed` / `healthy` branches in `_RECOVERY_SCRIPT`. Misconfigured rendering uses rewritten copy (heading "Workspace misconfigured", body explains services.toml is missing the `system_interface` entry), shows the structured checklist always-visible, hides the spinner, swaps the primary button for the secondary "Try restart anyway" host-restart button.
  - Add the structured checklist component: six rows for Q1-host/Q2-SSH/Q1-services-agent/Q4-decl/Q6-in-container/Q7-resolver, each with a pass/fail/warn icon and a one-line label. Render on the misconfigured page always-visible; render inside the `<details>` block on every other recovery state.
  - Add the SSH connection string list to the `<details>` block (one row per host with user@host, port, key path, and a per-row "Copy" button).
  - Add the page-level "Copy diagnostics" button that copies a JSON-ish flat dump of all probe results to the clipboard via the standard clipboard API.
  - JS-side: when `runProbe()` (the existing layer-2 trigger) receives the extended endpoint response and `is_misconfigured` is set, switch to `renderMisconfigured()` rather than dispatching surgical. When `ssh_dead` is set with `reachable` true, render the shared "Workspace unresponsive" page with the structured checklist visible and the primary button rebound to host restart (no auto-dispatch — user clicks to proceed).
- **`apps/minds/imbue/minds/desktop_client/system_interface_health.py`**
  - On the STUCK→HEALTHY transition (`record_probe_success` while non-HEALTHY), invoke a new optional on-recovery callback the app wires to the loguru INFO write described above. Falls back to no-op if the callback is unset.
- **Plumbing**
  - The new envelope flows through the existing `EnvelopeStreamConsumer` queue / dispatcher — no new threads, no new sync primitives.
  - The probe-script execution reuses `_capture_mngr_command` / `_run_mngr_subprocess` from `app.py`; no new subprocess plumbing.
  - All new state is bounded by the host-health endpoint's request/response — no new long-lived caches.
- **Phasing (single PR, multiple commits in this order)**
  1. Add `ResolverSnapshotPayload` to `mngr_forward`, emit it on resolver mutation, write the schema + unit tests. Minds-side consumer + accessor; debug-menu wiring rendering Q7 from in-memory state (no behavior change yet).
  2. Add the batched `mngr exec` probe + host-health endpoint extensions returning the parsed record (still no behavior change — debug menu now populated, but tier logic unchanged).
  3. Add the misconfigured tier classification + JS branch + rewritten copy + structured checklist. From here the recovery page can render the new state.
  4. Add the SSH-dead path: endpoint reports `ssh_dead`, JS renders the shared unresponsive page with the host-restart primary button (no auto-dispatch).
  5. Add the STUCK→HEALTHY loguru log of final probe results.
- **Testing**
  - Unit tests under `apps/minds/imbue/minds/desktop_client/`:
    - `templates_test.py` — render the recovery page in each of the new states (misconfigured, ssh_dead, debug-menu populated) and assert key DOM hooks.
    - `system_interface_health_test.py` — exercise the on-recovery callback wiring.
    - new `recovery_probe_test.py` — mock `mngr exec` stdout (sentinel present / absent / each Q4-Q6 combination) and `mngr list` JSON, exercise the endpoint's response classification, exercise the misconfigured gate.
  - Plugin unit tests in `libs/mngr_forward/imbue/mngr_forward/` — emit `ResolverSnapshotPayload` on mutation, assert serialization shape.
  - One integration test under `apps/minds/imbue/minds/desktop_client/test_desktop_client.py` driving an in-process FastAPI app: stub the batched probe to return a "missing services.toml entry" payload, hit the recovery page, assert the misconfigured copy renders and no restart is auto-dispatched. Pair it with a "happy path" assertion that an entry-present payload still auto-dispatches surgical.
  - Changelog entries under `apps/minds/changelog/gabriel-workspace-restart.md` and `libs/mngr_forward/changelog/gabriel-workspace-restart.md` (extending existing entries on this branch).
