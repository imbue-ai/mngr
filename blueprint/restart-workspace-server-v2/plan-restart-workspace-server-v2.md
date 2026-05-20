# Re-implement restart-system-interface on top of mngr_forward
> Note: the "workspace server" feature has since been renamed to "system interface". Symbol and route names below (`WorkspaceServerHealthTracker`, `workspace_backend_failure`, `workspace_server_health.py`, `restart-workspace-server`, ...) describe the plan as written before that rename.

## Overview

- Port the workspace-server health-tracking + restart-recovery feature from the old `restart-workspace-server` branch onto main's `libs/mngr_forward` plugin architecture, where HTTP/WS forwarding now lives in a separate process from `apps/minds/`.
- Preserve `mngr_forward`'s narrow charter: it stays a dumb reverse proxy + auth bridge. Its only new responsibility is *observation* — when it sees a backend failure (connect error, mid-SSE EOF, 5xx response) it emits a new envelope event on its stdout JSONL stream.
- Keep all interpretation, state, and UX in `apps/minds/`: tracker, restart endpoint, recovery page, chrome banner, landing/sidebar affordances.
- Cross the plugin → minds origin boundary by 302-redirecting HTML 503s to `localhost:<minds>/agents/{id}/recovery?return_to=...`. The URL bar isn't visible in the desktop client, so cross-origin redirect is fine.
- Restart mechanism is unchanged from the old branch: SSH into the agent and `tmux kill-window` on the workspace_server window + `touch /code/services.toml`. That mechanism lives in `forever-claude-template` (the agent container) and is independent of main's mngr_forward refactor.

## Expected behavior

- When the workspace server is responsive, the user sees no health-related UI. Nothing changes from today.
- When the plugin observes a backend failure for an agent, it emits a `workspace_backend_failure` observation envelope on its stdout. Minds' envelope consumer feeds this into the `WorkspaceServerHealthTracker`.
- After ≥5 seconds of continuous failures with no intervening success, the tracker moves the agent from HEALTHY to STUCK.
- On STUCK:
  - The chrome page (which subscribes to the existing `/chrome/events` SSE) shows a minimal banner above the iframe for the currently-displayed agent: "Workspace server unresponsive. Click to recover." Clicking navigates the iframe to `/agents/{id}/recovery`.
  - Any HTML navigation that hits the plugin's 503 path is 302'd to `/agents/{id}/recovery?return_to=<original_url>` on the minds origin. The recovery page requires minds auth (existing session is reused).
  - The landing page marks the agent's row as stuck and offers a one-click restart.
  - The sidebar context menu on the agent's row exposes a Restart entry.
- The recovery page shows a Restart button and subscribes to `/chrome/events` (filtered by `agent_id`) so it can reload to the `return_to` URL the moment the tracker reports HEALTHY again.
- Clicking Restart POSTs to `/api/agents/{id}/restart-workspace-server`. The endpoint:
  - Flips the tracker to RESTARTING (broadcast over SSE; chrome banner updates).
  - Runs the kill-window + touch commands via paramiko SSH (or a local subprocess for non-SSH agents).
  - Blocks until the workspace responds 200 (using a shared probe helper extracted from `agent_creator._wait_for_workspace_ready`) or hits a timeout; returns 200 on success.
- While the tracker is STUCK or RESTARTING, minds probes the agent through the plugin's subdomain on an interval; the first 200 flips the tracker back to HEALTHY (broadcast over SSE; banner clears; recovery page reloads to `return_to`).
- WebSocket failures are not recorded in v1; transient HTTP 503s within the 5-second window do not surface recovery UI.

## Changes

### `libs/mngr_forward/`

- `imbue/mngr_forward/envelope.py`: add a new emission method (e.g. `emit_workspace_backend_failure(agent_id, reason, status_code)`) that writes a `{"stream": "observe", "payload": {"kind": "workspace_backend_failure", "agent_id": ..., "reason": ..., "status_code": ..., "timestamp": ...}}` line on stdout. `reason` is one of `connect_error`, `sse_eof`, `5xx_response`.
- `imbue/mngr_forward/server.py`: thread the `EnvelopeWriter` into the workspace-forwarding code path. Call the new emitter from:
  - `_forward_workspace_http` on `httpx.ConnectError` / `httpx.RemoteProtocolError` before returning `_service_unavailable_response` (HTTP path).
  - `_forward_workspace_http` on `httpx.ConnectError` before returning `_service_unavailable_response` (SSE setup path).
  - `_forward_workspace_http` inside `_stream()` on `httpx.ReadError` / `httpx.RemoteProtocolError` / `httpx.TimeoutException` (mid-SSE EOF path).
  - `_handle_workspace_forward_http` when `resolver.resolve(agent_id)` returns `None` (backend not registered).
  - Any non-2xx/non-3xx response from the backend (5xx path).
- `imbue/mngr_forward/server.py`: update `_service_unavailable_response` to return a 302 to `http://localhost:<minds_origin>/agents/<agent_id>/recovery?return_to=<original_url>` when the request `Accept`s text/html. Non-HTML callers still get plain 503. The minds origin is sourced from configuration passed in at app construction (similar to how the plugin already knows its own port).

### `apps/minds/imbue/minds/desktop_client/`

- `workspace_server_health.py` (new): `AgentHealth` enum (`HEALTHY`, `STUCK`, `RESTARTING`) + thread-safe `WorkspaceServerHealthTracker`. Methods: `record_failure(agent_id)`, `record_success(agent_id)`, `mark_restarting(agent_id)`, `get_health(agent_id)`, `snapshot_all()`, `add_on_change_callback(callback)`. Policy: a first failure starts a 5-second window; if no success arrives within that window, the agent transitions to STUCK and the on-change callback fires. State transitions deduplicate (no spurious callbacks on identical transitions).
- `app.py`:
  - Construct a single `WorkspaceServerHealthTracker` at startup and stash it in `app.state`.
  - Subscribe to the envelope consumer's new `workspace_backend_failure` events; route them into the tracker's `record_failure`.
  - Add a background probe task that, for each agent currently STUCK or RESTARTING, probes its workspace through the plugin's subdomain (reusing the helper extracted from `agent_creator._wait_for_workspace_ready`); a successful probe calls `record_success`.
  - `POST /api/agents/{id}/restart-workspace-server`: marks the tracker RESTARTING, builds the tmux+touch command, dispatches via paramiko SSH using info already available in the backend resolver (via `reverse_tunnel_established` envelopes) or via local subprocess for local agents, then waits (using the shared workspace-ready helper) until the workspace responds 200 or times out. Returns 200 on success; on timeout, returns 504 and leaves the tracker in RESTARTING (the background probe will eventually flip it).
  - `GET /agents/{id}/recovery`: renders `recovery.html`. Requires minds auth (reuses existing session middleware).
  - `/chrome/events` SSE: push `workspace_server_status` events on every tracker state transition; payload includes `agent_id` and `status`.
- `backend_resolver.py`: add `get_work_dir(agent_id)` to `BackendResolverInterface` and `MngrCliBackendResolver`; returns the local work directory for non-SSH agents so the restart endpoint can `touch` a local services.toml without going through SSH.
- `ssh_tunnel.py`: extract `exec_remote_command(host, cmd) -> (exit_status, stderr)` from existing `write_api_url_to_remote` logic; the restart endpoint uses this for the SSH path. Add a focused unit test.
- `templates/recovery.html` (new): minimal page with a "Restart" button + status text. JS opens an `EventSource` to `/chrome/events`, filters for the current `agent_id`, and reloads to `return_to` (read from query string) when `workspace_server_status: HEALTHY` arrives. Button POSTs to `/api/agents/{id}/restart-workspace-server`. Re-entry guard on the SSE/poll loop.
- `templates/chrome.html`: render a banner element (initially hidden, anchored above the iframe). Subscribe its JS to `/chrome/events`; show banner text "Workspace server unresponsive. Click to recover." when the currently-displayed agent transitions to STUCK or RESTARTING; click navigates the iframe to `/agents/{id}/recovery`. Banner hides on HEALTHY.
- `static/landing.js`: per-row probing of each workspace's health on the landing page. Rows backed by an agent currently STUCK are visually marked. Clicking a stuck row issues the restart POST and waits for HEALTHY via SSE before navigating.
- `static/sidebar.js`: add a "Restart workspace server" entry to the existing right-click context menu on a workspace row. Posts to the restart endpoint.

### `apps/minds/imbue/minds/desktop_client/forward_cli.py`

- `EnvelopeStreamConsumer`: handle the new `workspace_backend_failure` observation envelope and dispatch it to a registered callback (which `app.py` wires to the tracker). Logging on malformed lines stays as-is.

### `libs/mngr/imbue/mngr/hosts/host.py`

- Local `Host` fast-path: reject `_retries` / `_retry_delay` / `_retry_until` kwargs loudly so that callers that accidentally pass them (e.g. SSH-shaped code paths) fail fast rather than silently ignoring retry intent.

### `apps/minds/imbue/minds/desktop_client/agent_creator.py`

- Extract the workspace-ready probe loop (currently inline in `_wait_for_workspace_ready`) into a shared module-level helper so the tracker's background probe and the restart endpoint can reuse it. Same semantics; same timeouts.

### Tests

- `workspace_server_health_test.py` (new, unit): tracker state transitions, 5-second STUCK threshold, success-clears, RESTARTING handling, on-change callback dedup, thread safety.
- `ssh_tunnel_test.py` (extended, unit): `exec_remote_command` returns `(exit_status, stderr)` correctly; failure paths.
- `templates_test.py` (extended, unit): renders `recovery.html` with expected anchors/IDs/return_to handling.
- `test_desktop_client.py` (extended, integration): full restart flow — failure envelope → STUCK → restart endpoint → workspace ready → HEALTHY; recovery page returns expected HTML; chrome banner SSE payload shape; auth required on recovery route; landing-row visual state on STUCK; sidebar context-menu entry rendering.
- `libs/mngr_forward/imbue/mngr_forward/server_test.py` (extended, unit): plugin emits failure envelope on each of the four failure paths; 302 to minds recovery URL on HTML 503s; non-HTML 503 path unchanged.
- One `@pytest.mark.tmux` test exercises a real local restart (`tmux kill-window` + `touch services.toml`) against a fake services.toml-watching subprocess.
- Ratchet adjustments: tighten any minds ratchets that decreased on the old branch (e.g. `broad_exception_catch`) — re-validate counts after the port.

### Changelog

- `changelog/restart-workspace-server-v2.md`: brief user-visible description of the recovery UX and restart endpoint (stub already committed; expand on final commit).

## Open questions

- **Restart endpoint timeout policy.** Two reasonable choices that the user left open:
  - 15s blocking with 504 on timeout (background probe takes over).
  - Fire-and-forget 202 immediately after kill+touch succeeds; tracker drives HEALTHY signal entirely via the probe.

  Either works given the SSE channel exists. Resolve during implementation by trying 15s blocking first and measuring how often real restarts exceed it.

- **Plugin-side 5xx detection threshold.** Should the plugin emit a `workspace_backend_failure` envelope on every 5xx, or only on 502/503/504 (the "infrastructure" subset)? A wedged Python backend often returns 500s with a stack trace — surfacing those as failures might be too aggressive. Default to 502/503/504 only.

- **Probe interval while STUCK/RESTARTING.** The shared workspace-ready helper currently polls every 1s during agent creation. The same cadence inside an idle tracker would generate steady traffic. Choose between 1s (matches creation), 2s (matches old branch's recovery poll), or back-off (1s → 2s → 5s). Default to 2s for v1.

- **Minds origin URL in plugin config.** The plugin needs to know the minds origin to construct the 302 target. Plumb this via `ForwardSubprocessConfig` (extend with a `minds_origin: str` field) and pass through to the FastAPI app's state, or hardcode `localhost:<minds_default_port>` and override via env var. Default: extend the config.

✓ Explore  ✓ Plan  ● Write  ○ Refine
