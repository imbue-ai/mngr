# Minds REST API

## Overview

* The minds desktop client currently exposes features through a mix of HTML page routes and ad-hoc JSON endpoints in `desktop_client/app.py`. There is no structured, versioned API that agents or external tools can program against.
* This spec adds a proper REST API under `/api/v1/`, mounted as a new FastAPI router into the existing app, covering: Cloudflare forwarding toggle, Telegram bot setup, and user notifications.
* Each agent created through the desktop client receives a unique API key (UUID4) injected as `MINDS_API_KEY` at creation time. The server stores only a SHA-256 hash, keyed by agent ID, and uses it to authenticate and identify callers.
* Agents on remote hosts reach the minds server through reverse SSH port forwarding. Local agents reach it directly. Both discover the URL via a well-known file at `$MNGR_AGENT_STATE_DIR/minds_api_url`.
* Notifications are routed through Electron (stdout JSONL) when running inside the desktop app, or through a tkinter toast popup when running standalone. The calling agent's identity is included in the notification.

## Expected Behavior

### REST API (`/api/v1/`)

* A new `APIRouter` is mounted at `/api/v1/` in the existing FastAPI app created by `create_desktop_client()`.
* All `/api/v1/` routes require authentication via `Authorization: Bearer <api_key>` header. Auth is implemented as a FastAPI dependency that hashes the provided key with SHA-256 and scans per-agent hash files to find a match.
* The API key grants full access to all `/api/v1/` endpoints (same authorization level as the session cookie). The hash identifies which agent made the call. Scoped restrictions are deferred.
* Routes:
  - `PUT /api/v1/agents/{agent_id}/servers/{server_name}/cloudflare` -- enable Cloudflare forwarding for a server (request body: `{"service_url": "..."}` or empty to auto-detect from backend resolver)
  - `DELETE /api/v1/agents/{agent_id}/servers/{server_name}/cloudflare` -- disable Cloudflare forwarding for a server
  - `POST /api/v1/agents/{agent_id}/telegram` -- start Telegram bot setup (request body: `{"agent_name": "optional"}`)
  - `GET /api/v1/agents/{agent_id}/telegram` -- get Telegram setup status
  - `POST /api/v1/notifications` -- send a notification to the user (request body: `{"title": "optional", "message": "required", "urgency": "low|normal|critical"}`)
* Agent CRUD and server listing routes are intentionally deferred.

### API Key Generation and Storage

* `_build_mngr_create_command()` in `agent_creator.py` generates a UUID4 API key and appends `--env MINDS_API_KEY=<uuid>` to the mngr create command. The function returns both the command list and the generated key.
* After `_build_mngr_create_command()` returns, the caller (`_create_agent_background`) hashes the key with SHA-256 and writes the hash to `~/.minds/agents/{agent_id}/api_key_hash` (plain text file containing the hex digest).
* On each authenticated request, the auth dependency hashes the Bearer token and iterates over all `~/.minds/agents/*/api_key_hash` files to find a match. If found, the corresponding agent ID is extracted from the path and made available to route handlers.

### Notifications

* `POST /api/v1/notifications` accepts `{"title": "optional string", "message": "required string", "urgency": "low|normal|critical"}`.
* The notification includes the calling agent's display name (resolved from `BackendResolverInterface.get_agent_display_info()`) or agent ID if the name is not discoverable.
* **Electron path**: if `MINDS_ELECTRON=1` is set in the server's environment, the notification is written to stdout as JSONL: `{"event": "notification", "title": "...", "message": "...", "urgency": "...", "agent_name": "..."}`. The Electron main process (`main.js`) parses this event from the backend's stdout stream and displays it via `new Notification()`.
* **Tkinter fallback**: if `MINDS_ELECTRON` is not set, a tkinter toast window is shown in a background thread. The window is small, always-on-top, positioned in the bottom-right corner of the screen, with a colored urgency indicator (green for low, yellow for normal, red for critical). It stays open until the user clicks to dismiss.
* No rate limiting for now.

### Reverse Port Forwarding (Agent -> Server Connectivity)

* `SSHTunnelManager` is extended with reverse port forwarding capability alongside its existing forward tunneling.
* When a remote agent is discovered and SSH info becomes available (via `MngrStreamManager`), the tunnel manager sets up a reverse port forward: it calls `transport.request_port_forward("127.0.0.1", 0)` on the paramiko SSH connection. The remote sshd picks a free port and returns it.
* The assigned remote port is used to write `http://127.0.0.1:{remote_port}` to `$MNGR_AGENT_STATE_DIR/minds_api_url` on the remote host, using the paramiko SSH connection directly (via `exec_command` or SFTP).
* For local agents, the URL file is written at discovery time with `http://127.0.0.1:{server_port}` (the minds server's actual port), to the local `$MNGR_AGENT_STATE_DIR/minds_api_url`.
* A background health-check thread runs every 30 seconds. For each active reverse tunnel, it verifies the SSH transport is alive. If broken, it re-establishes the reverse tunnel (potentially with a new remote port) and updates the URL file.
* The `SSHTunnelManager.cleanup()` method is extended to also tear down reverse tunnels.

### Electron Integration

* The Electron launcher (`backend.js`) is updated to set `MINDS_ELECTRON=1` in the environment when spawning the Python backend process.
* The Electron main process (`main.js`) is updated to handle `notification` events from the backend's stdout JSONL stream, displaying them via `new Notification({title, body})`.

## Implementation Plan

### New files

* **`imbue/minds/desktop_client/api_v1.py`** -- The `/api/v1/` router module. Contains:
  - `ApiKeyAuth` FastAPI dependency: extracts Bearer token from Authorization header, hashes with SHA-256, scans `~/.minds/agents/*/api_key_hash` files, returns `AgentId` of the caller or raises 401
  - `create_api_v1_router()` factory function that accepts the same dependencies as `create_desktop_client()` (backend resolver, cloudflare client, telegram orchestrator, notification dispatcher, paths) and returns an `APIRouter`
  - Route handlers for all 5 endpoints listed above

* **`imbue/minds/desktop_client/api_key_store.py`** -- API key hash storage. Contains:
  - `generate_api_key() -> str` -- generates a UUID4 string
  - `hash_api_key(key: str) -> str` -- SHA-256 hex digest
  - `save_api_key_hash(data_dir: Path, agent_id: AgentId, key_hash: str) -> None` -- writes to `~/.minds/agents/{agent_id}/api_key_hash`
  - `find_agent_by_api_key(data_dir: Path, key: str) -> AgentId | None` -- hashes key, scans all hash files, returns matching agent ID or None

* **`imbue/minds/desktop_client/notification.py`** -- Notification dispatch. Contains:
  - `NotificationUrgency` enum: `LOW`, `NORMAL`, `CRITICAL`
  - `NotificationRequest` pydantic model: `title: str | None`, `message: str`, `urgency: NotificationUrgency`
  - `NotificationDispatcher` class: takes `is_electron: bool` and `output_format: OutputFormat` at construction. `dispatch(request, agent_display_name)` method routes to Electron (stdout JSONL via `emit_event`) or tkinter.
  - `_show_tkinter_toast(title, message, urgency, agent_name)` -- spawns a background thread that creates a small always-on-top tkinter window in the bottom-right corner with colored urgency indicator (green/yellow/red), stays until clicked to dismiss

### Modified files

* **`imbue/minds/desktop_client/agent_creator.py`**:
  - `_build_mngr_create_command()` signature changes to also return the generated API key: `-> tuple[list[str], str]`. It generates a UUID4, appends `--env MINDS_API_KEY=<key>` to the command, and returns both.
  - `_create_agent_background()` receives the key from `_build_mngr_create_command()`, hashes it, and saves the hash via `save_api_key_hash()`.

* **`imbue/minds/desktop_client/ssh_tunnel.py`**:
  - `SSHTunnelManager` gets new methods:
    - `setup_reverse_tunnel(ssh_info, local_port) -> int` -- calls `transport.request_port_forward("127.0.0.1", 0)` and returns the assigned remote port. Stores the tunnel metadata for health checking.
    - `write_api_url_to_remote(ssh_info, agent_state_dir, url)` -- writes the URL string to `{agent_state_dir}/minds_api_url` on the remote host via the paramiko connection (exec_command).
    - `write_api_url_to_local(agent_state_dir, url)` -- writes the URL string to the local filesystem at `{agent_state_dir}/minds_api_url`.
    - `start_reverse_tunnel_health_check()` -- starts a daemon thread that runs every 30 seconds, checks each reverse tunnel's SSH transport, re-establishes and updates URL files if broken.
    - `cleanup()` is extended to cancel reverse port forwards and stop the health-check thread.
  - New private attrs for tracking reverse tunnels: `_reverse_tunnels: dict[str, ReverseTunnelInfo]` mapping host key to tunnel metadata (ssh_info, local_port, remote_port, agent_state_dir).

* **`imbue/minds/desktop_client/app.py`**:
  - `create_desktop_client()` accepts a new `notification_dispatcher: NotificationDispatcher | None` parameter.
  - Creates the `/api/v1/` router via `create_api_v1_router()` and mounts it with `app.include_router(router, prefix="/api/v1")`.

* **`imbue/minds/desktop_client/runner.py`**:
  - `start_desktop_client()` creates a `NotificationDispatcher` (checking `os.getenv("MINDS_ELECTRON")`) and passes it to `create_desktop_client()`.
  - After the `MngrStreamManager` discovers agents, triggers reverse tunnel setup and URL file writing for remote agents, and direct URL file writing for local agents. This likely involves a callback or observer on agent discovery events.

* **`imbue/minds/desktop_client/backend_resolver.py`**:
  - `MngrStreamManager` gains a hook or callback for agent discovery events so that the runner can trigger reverse tunnel setup when a new remote agent appears.

* **`electron/backend.js`**:
  - Add `MINDS_ELECTRON: '1'` to the env object passed to `spawn()`.

* **`electron/main.js`**:
  - In the stdout JSONL parsing loop, handle `event === 'notification'` by calling `new Notification({title: event.title, body: event.message})`.

### New primitives / data types

* `NotificationUrgency` enum in `notification.py` (or `primitives.py` if reused elsewhere)
* `ApiKeyHash` primitive (a `NonEmptyStr` subclass) in `primitives.py`

## Implementation Phases

### Phase 1: API Key Infrastructure

* Create `api_key_store.py` with key generation, hashing, storage, and lookup functions.
* Modify `_build_mngr_create_command()` to generate and inject the API key.
* Modify `_create_agent_background()` to persist the hash after agent creation.
* Add `ApiKeyHash` primitive to `primitives.py`.
* Result: agents are created with API keys and hashes are stored. No routes yet, but the auth infrastructure is in place.

### Phase 2: REST API Router and Auth

* Create `api_v1.py` with the `ApiKeyAuth` dependency and `create_api_v1_router()`.
* Implement the Cloudflare forwarding routes (`PUT`/`DELETE`), wiring to the existing `CloudflareForwardingClient`.
* Implement the Telegram routes (`POST`/`GET`), wiring to the existing `TelegramSetupOrchestrator`.
* Mount the router in `create_desktop_client()`.
* Result: the `/api/v1/` routes are live and authenticated. Agents with API keys can toggle Cloudflare forwarding and set up Telegram bots.

### Phase 3: Notifications

* Create `notification.py` with `NotificationDispatcher`, the Electron stdout path, and the tkinter toast implementation.
* Add the `POST /api/v1/notifications` route to the API router.
* Update `runner.py` to create the dispatcher and pass it through.
* Update `electron/backend.js` to set `MINDS_ELECTRON=1`.
* Update `electron/main.js` to handle notification events from stdout.
* Result: agents can send notifications that appear as native Electron notifications or tkinter toasts.

### Phase 4: Reverse Port Forwarding and URL File

* Extend `SSHTunnelManager` with reverse tunnel methods, health checking, and URL file writing.
* Hook into `MngrStreamManager` agent discovery to trigger reverse tunnel setup for remote agents and direct URL file writing for local agents.
* Update `runner.py` to wire the discovery callback and pass the server port to the tunnel manager.
* Result: all agents (local and remote) can read `$MNGR_AGENT_STATE_DIR/minds_api_url` to discover the minds server and call its API.

## Testing Strategy

### Unit tests

* `api_key_store.py`: test key generation (valid UUID4), hashing (deterministic SHA-256), save/load round-trip, `find_agent_by_api_key` with multiple agents, and not-found case.
* `notification.py`: test `NotificationDispatcher` routes to Electron (mock stdout, verify JSONL output) or tkinter (mock tkinter, verify window creation) based on `is_electron` flag. Test urgency enum validation.
* `api_v1.py`: test auth dependency rejects missing/invalid Bearer tokens. Test each route handler with mocked dependencies (cloudflare client, telegram orchestrator, notification dispatcher).
* `ssh_tunnel.py` (reverse tunnel): test `setup_reverse_tunnel` calls `request_port_forward` correctly, test URL file writing (both local and remote via mocked paramiko), test health-check thread re-establishes broken tunnels.

### Integration tests

* Create an agent via `AgentCreator`, verify `MINDS_API_KEY` env var is set on the mngr command and hash file is written.
* Start the desktop client app (via test client), authenticate with a valid API key, and verify the Cloudflare and Telegram endpoints respond correctly.
* Verify that an invalid API key returns 401.
* Verify the notification endpoint writes correct JSONL to stdout when `MINDS_ELECTRON=1`.

### Edge cases

* API key file does not exist or is corrupted -- should return 401, not crash.
* Cloudflare client is None (not configured) -- Cloudflare routes return 501.
* Telegram orchestrator is None -- Telegram routes return 501.
* SSH connection drops during reverse tunnel health check -- re-establish cleanly without affecting forward tunnels.
* Multiple reverse tunnels to the same host -- should reuse the SSH connection but create separate port forwards per agent.
* Agent discovered before server port is known -- defer reverse tunnel setup until server is ready.
* Tkinter not available (headless server) -- notification dispatch should log a warning and not crash.

## Open Questions

* **Agent state dir discovery for local agents**: the server needs to know the local `$MNGR_AGENT_STATE_DIR` path for each agent to write the URL file. This path follows the convention `$MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/`, but `MNGR_HOST_DIR` defaults to `~/.mngr/` on the local machine. Should this be hardcoded to the convention, or discovered from `mngr` somehow?
* **Reverse tunnel and shared hosts**: multiple agents can share a single host. Should one reverse tunnel per host suffice (writing the URL file for each agent on that host), or one per agent?
* **stdout contention**: the Electron notification path writes JSONL to stdout. The `emit_event` utility already writes to stdout. If notifications arrive concurrently (multiple agents notifying at once), is there a risk of interleaved writes? May need a stdout lock.
* **Tkinter and macOS**: tkinter on macOS has restrictions around running on the main thread. Since the desktop client's main thread runs uvicorn, the tkinter toast will run in a background thread -- this may require `root.after()` scheduling or other workarounds on macOS.
