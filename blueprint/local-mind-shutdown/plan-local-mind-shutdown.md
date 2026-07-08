# Plan: Local-mind shutdown on quit + landing-page Start/Stop controls

## Overview

- Goal: stop letting local minds silently consume the user's machine after the app is closed, and give users direct Start/Stop control from the landing page.
- Two user-facing additions:
  1. On app quit, if any *local* minds (containers) are still running, prompt the user to shut them down or leave them running.
  2. On the landing page, show each local mind's container status and a per-state Start/Stop button.
- "Local mind" = a workspace whose host runs on a local provider backend (`docker` or `lima`). Remote minds (Modal, OVH, etc.) are never counted or stopped — they don't use this machine's resources.
- New capability: a dedicated, lightweight **local-liveness poll** in the minds backend that tracks whether each local mind's container is up. It is separate from the global discovery snapshot (which fans out across all providers every 300s and is intentionally slow); the new poll only touches local hosts, so it can run cheaply once a minute.
- The poll is **overridden immediately** when the user stops or starts a mind through minds itself, so the UI reflects the new state at once instead of waiting for the next tick.
- Stop = `mngr stop --stop-host` (bounces the container, preserves data, fully restartable). Start = `mngr start` (boots the stopped container). Neither destroys anything.
- Decision (dropped from scope): no "remember this choice" preference for now — the quit prompt appears every time there are running local minds.

## Expected behavior

### Local-liveness status

- The backend continuously knows, within ~1 minute, whether each local mind's container is `running`, `stopped`, or `unknown`.
- When the user stops/starts a mind from minds, the status updates immediately (no wait for the next poll); externally-driven changes (e.g. a manual `docker stop`) reflect by the next poll tick.
- Status is pushed to every open window over the existing chrome SSE, the same way system-interface health already is.
- Remote minds receive no container status (the concept doesn't apply to them).

### Landing page

- Each **local** mind row shows a container-status badge:
  - Running → no extra badge (normal), action button = **Stop**.
  - Stopped → "Stopped" badge, action button = **Start**.
  - Unknown → "Status unknown" badge, no Start/Stop action.
  - Transient `Stopping…` / `Starting…` states are shown optimistically while a command the user just issued is in flight.
- When a local mind is **Stopped**, the system-interface health badge ("Server not responding" / "Restarting…") is suppressed — the server is down by definition, so that badge would be noise.
- The dedicated **Restart** button is removed from local rows; restart/recovery remains reachable by clicking an unhealthy row (existing behavior). **Remote** rows are unchanged (they keep the Restart button).
- **Stop** opens a native confirmation dialog ("Stop this mind? Its agents will stop and its services become inaccessible. Data is preserved and you can start it again."). On confirm, the mind is stopped; the row flips to Stopped.
- **Start** runs in the background with no navigation — the row flips Starting… → Running. To actually open the mind, the user clicks the row as they do today.
- The Settings button stays on every row.

### Quit

- On `Cmd/Ctrl+Q` (or last-window-close / SIGTERM), before shutting the backend down, the app checks for running local minds.
- If none are running: quit proceeds exactly as today.
- If one or more are running: a dialog appears stating how many and which minds are running, with the message that leaving them running keeps using computer resources, while shutting them down stops their agents and makes their services inaccessible. Options:
  - **Cancel** — abort the quit, app stays open.
  - **Leave running** — quit immediately; containers keep running in the background (today's behavior).
  - **Shut down** — stop every running local mind, then quit.
- When **Shut down** is chosen, the app shows a "Stopping minds…" progress state and waits for all stops to finish before exiting.
- If a stop fails, the app surfaces which mind(s) failed and lets the user **Retry** or **Cancel the quit** (it does not silently exit leaving a half-stopped state).
- The quit prompt uses a fresh liveness check at quit time (not just the last cached poll) so it never lists a mind that was already stopped.

## Changes

### Backend — minds (`apps/minds`)

- Add a **local-liveness tracker** (analogous to the existing system-interface health tracker) holding the latest container state per local mind, with a registered change callback that wakes the chrome SSE.
- Add a **local-liveness poll loop** (background thread started in the backend lifespan): every ~60s, enumerate local-provider hosts and read their container state via a scoped, non-starting `mngr list` (the same read the host-health probe already uses), and update the tracker. The loop is wakeable on demand so a user-initiated stop/start triggers an immediate refresh instead of waiting a full minute.
- Add a helper to classify a mind as local by its provider backend (`docker` / `lima`), using provider info already available from discovery.
- Extend the chrome SSE (`/_chrome/events`) to emit a new per-mind `local_mind_state` event (initial snapshot on connect + on change), mirroring how `system_interface_status` is emitted today.
- Add two endpoints:
  - `POST /api/agents/{agent_id}/stop-host` — runs `mngr stop … --stop-host` on the workspace's host, then optimistically marks the mind stopped and triggers an immediate poll refresh.
  - `POST /api/agents/{agent_id}/start-host` — runs `mngr start …` on the host, then optimistically marks it starting and triggers a refresh.
- Add an endpoint the Electron quit flow can call to get the **currently-running local minds** (id + display name), forcing a fresh liveness read so the quit prompt is accurate.

### Landing page (`templates/pages/Landing.jinja`)

- Render Start/Stop/Restart buttons conditionally per row based on whether the mind is local and its container state (server-side initial state + client-side updates).
- Subscribe to the new `local_mind_state` SSE event; update the status badge and swap the action button as state changes, and apply optimistic `Stopping…` / `Starting…` states on click.
- Suppress the system-interface health badge for a row whose container is Stopped.
- Start button → background `fetch` to `start-host`, no navigation.
- Stop button → request a native confirmation via the Electron relay (below); on confirm, the stop is dispatched. Provide a plain in-page `confirm()` fallback for the non-Electron (browser) case so the control still works.

### Electron shell (`apps/minds/electron`)

- `content-relay-preload.js`: allowlist new `postMessage` types from the landing page (stop-mind, and optionally start-mind) and forward them to the main process — following the existing `minds:open-request-modal` relay pattern.
- `main.js`:
  - Add IPC handlers that, for a stop request, show the native confirmation dialog (like the sidebar's "Restart workspace?") and then call the backend `stop-host` endpoint.
  - Cache the latest `local_mind_state` per agent in `latestChromeState` (and prime new views), as is done for workspaces/health.
  - Rework the quit path (`before-quit` / `initiateFullQuit`): before `shutdown()`, query the backend for running local minds; if any, show the quit dialog. On "Shut down", dispatch stops, show a "Stopping minds…" progress state, await completion (with Retry / Cancel-quit on failure), then quit. On "Leave running", quit immediately. On "Cancel", abort the quit.

### Docs

- Update `apps/minds/docs/desktop-app.md` (Shutdown section) to describe the quit-time local-mind prompt, and note the landing-page Start/Stop controls and the local-liveness poll.

### Tests

- Backend unit tests: local-vs-remote classification; tracker state transitions; the `local_mind_state` SSE payload shape; `stop-host` / `start-host` endpoints dispatching the right `mngr` argv and triggering a refresh; the running-local-minds endpoint forcing a fresh read.
- Backend integration test: a stop/start round-trip drives the tracker and emits the expected SSE events.
- Landing-page template tests: correct button/badge per (local?, state) combination, and health-badge suppression when Stopped.
- Manual verification (per repo guidance, not crystallized): tmux-driven check of the Electron quit dialog (none-running vs some-running, Shut down / Leave running / Cancel, and the stop-failure Retry path).
