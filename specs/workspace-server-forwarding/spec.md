# Move Service Forwarding Into `minds_workspace_server`

## Overview

- Today the desktop client (`apps/minds/...`) owns service-worker injection, HTML absolute-path rewriting, cookie-path scoping, WebSocket URL shimming, and per-service path dispatch at `/forwarding/{agent_id}/{server_name}/...`. The `minds_workspace_server` frontend then embeds other workspace services as iframes on separate Cloudflare hostnames (`{service}--{id}--{user}.domain`).
- Those iframes fail when accessed through Cloudflare because each service is a different origin: cookies, storage, and service-worker scope don't cross origins, so the embedded services don't render correctly in a shared browser session.
- Move all path-rewriting / SW / cookie / WS machinery **into** `minds_workspace_server`, under a single `/service/<service-name>/...` prefix. System_interface keeps top-level paths for its own UI and API. When system_interface is shared via Cloudflare, all services are reachable on the same origin under path prefixes — iframes, cookies, and service workers all work correctly.
- Locally, the desktop client addresses each workspace by subdomain: `http://<agent-id>.localhost:8420/...` forwards byte-for-byte to that workspace's `minds_workspace_server` port. The desktop client shrinks to a pure reverse proxy + auth gate + chrome UI; it stops tracking per-service backends for proxying (it still tracks them for Cloudflare sharing toggles).
- Non-system_interface Cloudflare hostnames still point directly at raw service ports, unchanged — so per-service sharing to specific users continues to work exactly as today.
- Ships as a single atomic PR (including the `vendor/mngr` sync in `forever-claude-template`). No backwards compatibility with `/forwarding/...` URLs or multi-mode layout files; existing saved layouts are abandoned and rebuilt on first use.

## Expected Behavior

### From the end user's perspective

- Opening the Minds Electron app still lands on `http://localhost:8420/` with the existing login / accounts / workspace-list chrome.
- Selecting a workspace in the sidebar loads `http://<agent-id>.localhost:8420/` in the content view. The workspace's dockview UI (system_interface) renders identically to today.
- Iframe panels inside dockview load URLs like `/service/web/`, `/service/terminal/`, `/service/<plugin-service>/` — all same-origin under the workspace's subdomain.
- Sharing a system_interface via Cloudflare yields one public hostname (`system_interface--<id>--<user>.domain`). Anyone with Access to it reaches every service through the same origin via `/service/<name>/` paths — iframes, cookies, service workers all work.
- Sharing an individual non-system_interface service (e.g. `web`) continues to work exactly as today: the user gets a separate Cloudflare hostname (`web--<id>--<user>.domain`) that goes directly to the raw service port, bypassing workspace_server entirely.
- Unauthenticated HTML navigations to `<agent-id>.localhost:8420/*` 302-redirect to `http://localhost:8420/login?next=...`; non-HTML requests return 403.

### From the system's perspective

- `uvicorn` on the desktop client already binds `127.0.0.1:8420` and answers for any hostname that resolves to it. Middleware on each request reads `Host`, splits off the subdomain:
  - Empty subdomain (bare `localhost`) → existing desktop-client pages.
  - `<agent-id>.localhost` where `<agent-id>` is a known primary workspace → reverse-proxy all traffic to that workspace's `minds_workspace_server` port. Only the auth cookie check runs before forwarding.
  - Unknown subdomain → 404.
- `minds_workspace_server` owns three kinds of traffic on its port:
  1. Its own frontend and API at top-level paths (`/`, `/api/*`, `/plugins/*`, `/assets/*`, `/favicon.ico`, SPA catch-all).
  2. `/service/<name>/...` reverse-proxied to the corresponding backend URL from `runtime/applications.toml`, with SW bootstrap + HTML/cookie/WS rewriting applied.
  3. `/service/<name>/__sw.js` serves the scoped service worker for that service prefix.
- All services share one origin per workspace (locally `<agent-id>.localhost:8420`, via Cloudflare `system_interface--<id>--<user>.domain`), so cookie-path rewriting to `Path=/service/<name>/` is sufficient to keep service cookies isolated.
- The desktop client no longer dispatches or rewrites on a per-service basis. It maintains exactly one reverse connection per remote workspace (SSH tunnel to the workspace_server port only) instead of one-per-service.
- The desktop client still tracks per-service URLs (via `MngrStreamManager` streaming `mngr event ... services --follow`) for the Cloudflare sharing toggle UI and the settings pages — those directly call `remote_service_connector` with a raw service URL, which is unchanged.

### Behavior changes that users will notice

- Workspace URLs change. `http://localhost:8420/forwarding/<id>/system_interface/` is gone; the same UI lives at `http://<agent-id>.localhost:8420/`. Electron follows the new URL scheme automatically; users with bookmarks to the old scheme get broken links (no redirect).
- Saved workspace layouts reset once. The `layout-{mode}.json` file naming collapses to `layout.json`, and old files are abandoned (not migrated) because the URLs they contain are in the old scheme anyway.
- `minds-workspace-server` run standalone for dev no longer has three distinct access modes — everything uses relative `/service/<name>/` URLs. Developers connecting directly to the bare workspace_server port (e.g. `localhost:8000`) are expected to do so under the same scheme; the `dev`-specific iframe-URL construction is removed.

## Implementation Plan

All paths relative to repo root.

### New code in `apps/minds_workspace_server/imbue/minds_workspace_server/`

#### `proxy.py` (new)

Pure rewriting helpers — direct ports of the corresponding functions in `apps/minds/imbue/minds/desktop_client/proxy.py`, with `/forwarding/{agent_id}/{server_name}` replaced by `/service/{service_name}` throughout. `AgentId` is dropped from the signatures since there is only one workspace per server.

- `_get_service_prefix(service_name: ServiceName) -> str` — returns `/service/<name>`.
- `generate_bootstrap_html(service_name: ServiceName) -> str` — the first-navigation HTML that installs the scoped SW and sets a `sw_installed_<service_name>` cookie at `Path=/service/<name>/`.
- `generate_service_worker_js(service_name: ServiceName) -> str` — the SW JS, scoped to `/service/<name>/`, that prepends the prefix to in-page fetches missing it.
- `generate_websocket_shim_js(service_name: ServiceName) -> str` — the `new WebSocket(...)` shim that prepends the prefix to same-host WS URLs.
- `rewrite_cookie_path(set_cookie_header: str, service_name: ServiceName) -> str` — rewrites `Set-Cookie ... Path=...` to `Path=/service/<name>/...`.
- `rewrite_absolute_paths_in_html(html: str, service_name: ServiceName) -> str` — rewrites `href=/...`, `src=/...`, `action=/...`, `formaction=/...` to include the prefix.
- `rewrite_proxied_html(html: str, service_name: ServiceName) -> str` — combines absolute-path rewriting with `<base>` injection and the WS shim.
- `generate_backend_loading_html(current_service: ServiceName | None, other_services: tuple[ServiceName, ...]) -> str` — the auto-retrying loading page, with fallback links to other registered services (no agent-id since we're already on the right workspace).

#### `service_dispatcher.py` (new)

FastAPI handlers for the new `/service/<name>/...` routes plus a host-header validating middleware step. Mirrors structure of `apps/minds/.../desktop_client/app.py::_handle_proxy_http` and `_handle_proxy_websocket`, but strictly local (no SSH tunnel logic — services are always on `127.0.0.1`):

- `handle_service_http(service_name: str, path: str, request: Request) -> Response` — dispatches by looking up the service URL from `AgentManager`, handling the SW-bootstrap cookie handshake, and forwarding HTTP (streaming responses where `accept: text/event-stream`, buffered otherwise). Uses the single shared `httpx.AsyncClient` kept on `app.state.http_client`.
- `handle_service_websocket(websocket: WebSocket, service_name: str, path: str) -> None` — WebSocket forward with client↔backend byte-forwarding, using the existing `websockets` library.
- `_build_proxy_response(backend_response, service_name)` — applies cookie-path rewriting and HTML rewriting on outbound responses, using the helpers from `proxy.py`.
- `_make_loading_html(service_name, agent_manager)` — builds the retry page.
- `register_service_routes(application: FastAPI) -> None` — registers `/service/{service_name}/__sw.js` (served directly, never proxied), `/service/{service_name}/{path:path}` HTTP, and the matching WebSocket route.

#### `server.py` (existing, modified)

- Create a single `httpx.AsyncClient` in `_lifespan` and store on `application.state.http_client`; close on shutdown.
- Add a host-header middleware that only rejects requests with an unexpected `Host`; in practice this does nothing but is a defence-in-depth hook.
- Call `service_dispatcher.register_service_routes(application)` during `create_application`.
- Drop `_inject_agent_id_meta_tag` and the `minds-workspace-server-agent-id` meta tag — the frontend no longer needs to know.
- `_layout_filename()` always returns `"layout.json"` (drop the `?mode=` query param handling); keep the layout endpoints otherwise unchanged.
- `AgentManager` gains `get_service_url(service_name: str) -> str | None` and `list_service_names() -> tuple[str, ...]`, both backed by `_applications`. These are used by the dispatcher.

#### `primitives.py` / related modules (new or modified)

- New primitive `ServiceName` (NonEmptyStr subclass) in `imbue.minds_workspace_server.primitives` (new file) — mirrors the existing `ServerName` in `imbue.minds.primitives`.
- The on-disk events file stays at `events/services/events.jsonl` (renamed from `servers`). Event records carry `service` instead of `server`. The `app-watcher` in `forever-claude-template/libs/app_watcher/` is updated in the same PR.

#### `config.py` (modified)

- No new required config. Documented dev command `minds-workspace-server --port 8000` works as today; accessing a dev instance uses the same `/service/<name>/` URL convention.

### Modified code in `apps/minds/imbue/minds/desktop_client/`

#### Files deleted outright

- `proxy.py` — moved in full to `apps/minds_workspace_server/imbue/minds_workspace_server/proxy.py`.
- `proxy_test.py` — moved alongside (kept as unit tests for the pure helpers).
- `/forwarding/{agent_id}/servers/` and `/forwarding/{agent_id}/{server_name}/{path:path}` routes and their handlers in `app.py`.
- `_handle_proxy_http`, `_handle_proxy_websocket`, `_forward_http_request`, `_forward_http_request_streaming`, `_build_proxy_response`, `_make_loading_html`, `_connect_backend_websocket`, `_get_tunnel_http_client` helpers in `app.py`. `_get_tunnel_socket_path` stays (simplified) because the SSH tunnel still exists, just for one port.

#### `app.py` (modified)

- Add middleware that inspects `request.url.hostname`:
  - Bare `localhost` / `127.0.0.1` → existing handlers run unmodified.
  - `<agent-id>.localhost` → dispatches to a single new handler `_handle_workspace_forward(request)` that:
    1. Verifies the agent is a known primary workspace via `backend_resolver`.
    2. Checks the session cookie (`_is_authenticated`). If unauthenticated and `accept: text/html`, return `Response(status_code=302, headers={"Location": f"http://localhost:8420/login?next={quote(original_url)}"})`. Otherwise 403.
    3. Resolves the workspace_server URL (looks up `service_name="system_interface"` from the resolver), possibly via SSH tunnel.
    4. Byte-forwards the entire request to that URL (path + query + headers + body) using the shared `httpx.AsyncClient`. Streams response. No header/body rewriting.
  - Unknown subdomain → 404.
- WebSocket middleware equivalent: `_handle_workspace_forward_ws(websocket)` does the same resolve + byte-forward (no rewriting).
- `render_landing_page` workspace links point at `http://<agent-id>.localhost:8420/` instead of `/forwarding/{agent_id}/system_interface/`.
- `_handle_agent_default_redirect` is removed; the equivalent redirect target is now the bare `<agent-id>.localhost:8420/`.
- `create_session_cookie` in `cookie_manager.py` sets `Domain=localhost` on the `Set-Cookie` header so the cookie is sent on all subdomains. `verify_session_cookie` is unchanged.

#### `backend_resolver.py` (modified)

- `get_backend_url(agent_id, service_name)` remains, but only `system_interface` needs to return a usable URL for the proxy layer. Per-service URLs are still populated for Cloudflare sharing; add an explicit `get_workspace_server_url(agent_id)` convenience helper used by the proxy path.
- `list_servers_for_agent` → `list_services_for_agent` (rename).
- `ServerLogRecord` → `ServiceLogRecord`; `parse_server_log_record(s)` → `parse_service_log_record(s)`; file path constants renamed.

#### `ssh_tunnel.py` (modified)

- `SSHTunnelManager` API stays, but the desktop client now only ever requests one tunnel per remote workspace (to the workspace_server port). Delete the per-(agent, remote_port) keying docs/tests that assumed multiple tunnels per workspace; a single UDS path per workspace is sufficient.
- `_get_tunnel_socket_path` simplifies accordingly.

#### Other files

- `api_v1.py` — adjust any handler that used `server_name` in path params or request bodies to use `service_name`. The Cloudflare GET/POST/DELETE endpoints continue to accept a service name and pass through to `CloudflareClient.add_service(agent_id, service_name, service_url)` unchanged.
- `templates.py` — any internal link from `/forwarding/...` → `http://<agent-id>.localhost:8420/` / `/service/<name>/...`. The sharing editor and account/workspace pages keep their bare-origin paths.
- `minds_config.py` / `config/data_types.py` — no change.
- `test_desktop_client.py` — update the `_handle_agent_default_redirect` assertion to expect the bare-origin redirect, and delete `/forwarding/` test cases. Add a subdomain-routing test case.

### Frontend (`apps/minds_workspace_server/frontend/src/`)

#### `base-path.ts` (modified)

- Delete `getPrimaryAgentId()` and the `minds-workspace-server-agent-id` meta tag reference.
- `getBasePath()` and `getHostname()` retained but likely return empty string / the literal subdomain — still useful for `apiUrl()` when non-root-mounted.

#### `views/DockviewWorkspace.ts` (modified)

- Delete `getAccessMode()`, `getForwardingPrefix()`.
- `getApplicationUrl(appName, rawUrl)` → `getServiceUrl(serviceName)` returning `/service/<name>/`. One line.
- `getTerminalUrl()` returns `/service/terminal/` (plus any `?arg=...` workdir args appended).
- Remove the `hostname.match(/^[^-]+--(.*)/)` cloudflare-sniff branches in both helpers. Everything is relative.
- Remove references to the `<base href="/forwarding/...">` pattern.

#### `models/AgentManager.ts` (modified)

- If the frontend reads `applications` entries' `url` fields, rewrite to just use `/service/<name>/` and ignore the raw URL (the raw URL is only meaningful to the backend/sharing flow).

#### Layout endpoints

- Saved layouts: the `?mode=` query param is dropped. Frontend calls `GET/POST /api/agents/{id}/layout` with no query string. Old files on disk are ignored.

### `forever-claude-template`

#### `vendor/mngr`

- Full resync of the vendored `mngr` tree to the post-refactor version of this repo, via `/release-minds` or equivalent. Captures all Python and frontend changes.

#### `libs/app_watcher/src/app_watcher/watcher.py`

- Rename the `servers` event directory to `services` and the event field `server` to `service`. Delete any references to `/forwarding/` that might exist.

#### `services.toml`

- No change to the content; system_interface command still runs `forward_port.py --name system_interface ... && minds-workspace-server`.

#### `scripts/forward_port.py`

- No content change (atomic upsert into `applications.toml`). Does not require reserved-name validation per the spec.

### Tests

#### New unit tests (`apps/minds_workspace_server/imbue/minds_workspace_server/proxy_test.py`)

Direct port of `apps/minds/.../desktop_client/proxy_test.py`:
- `rewrite_cookie_path` (existing `Path`, no `Path`, `Path` already prefixed, edge-cases with extra spaces, multiple cookies)
- `rewrite_absolute_paths_in_html` (href/src/action/formaction, case variants, protocol-relative URLs untouched, already-prefixed URLs untouched)
- `generate_service_worker_js` / `generate_bootstrap_html` / `generate_websocket_shim_js` — snapshot contents match the expected prefix
- `generate_backend_loading_html` — with and without other services

#### New integration tests (`apps/minds_workspace_server/imbue/minds_workspace_server/service_dispatcher_test.py`)

Spin up a small stub FastAPI app on an ephemeral port, register it in `AgentManager` via a controlled `applications.toml`, and verify:
- `GET /service/stub/` → first navigation returns bootstrap HTML
- `GET /service/stub/__sw.js` → returns SW JS scoped to `/service/stub/`
- After setting the `sw_installed_stub` cookie, `GET /service/stub/path` proxies to the stub and rewrites absolute paths in the response
- `Set-Cookie: foo=bar; Path=/` from the stub is rewritten to `Path=/service/stub/`
- WebSocket `/service/stub/ws` proxies bidirectionally
- Unknown service `/service/nonexistent/` returns the loading page for `accept: text/html`, 502 otherwise
- SSE endpoints (stub streams) forward incrementally, not buffered

#### Modified integration tests (`apps/minds/imbue/minds/desktop_client/test_desktop_client.py`)

- Add cases exercising `GET http://<agent-id>.localhost:8420/...` routing via the new middleware with a fake workspace_server.
- Assert that an unauthenticated HTML navigation 302s to `http://localhost:8420/login?next=...`.
- Assert that the session cookie's `Set-Cookie` contains `Domain=localhost`.
- Remove all `/forwarding/` test cases.

#### Acceptance test (`apps/minds/test_desktop_client_e2e.py`)

Extend existing end-to-end fixture to spin up:
- A stub backend service on a local port
- A local workspace_server (via `minds-workspace-server`) that knows about the stub via `applications.toml`
- The desktop client

Then exercise: `GET http://<agent-id>.localhost:8420/` returns the workspace_server frontend HTML; `GET http://<agent-id>.localhost:8420/service/stub/` reaches the stub and rewrites a known `Set-Cookie`.

#### Ratchet updates

- `apps/minds/test_ratchets.py`: decrement counts for any ratchets affected by the deletion of `proxy.py` and proxy handlers.
- `apps/minds_workspace_server/imbue/minds_workspace_server/test_ratchets.py`: re-baseline for the added proxy code.

## Implementation Phases

The whole refactor ships in one PR, but the work is ordered to let us reach intermediate working states during development. Each phase is expected to be a separate commit.

### Phase 1 — Groundwork renames

- Rename `ServerName` → `ServiceName` in `apps/minds/.../primitives.py` (and re-export if needed).
- Rename `ServerLogRecord` → `ServiceLogRecord`, `server_log` helpers → `service_log`, event file path constants (`SERVERS_EVENT_SOURCE_NAME` → `SERVICES_EVENT_SOURCE_NAME`), `server` event field → `service`.
- Rename `list_servers_for_agent` → `list_services_for_agent` on the resolver.
- Update `app_watcher` and `forward_port.py` so registrations write to `events/services/events.jsonl` with `service:` keys.
- Runs pass end-to-end against the old `/forwarding/...` URL scheme after this phase.

### Phase 2 — Port rewriting helpers into workspace_server

- Create `apps/minds_workspace_server/imbue/minds_workspace_server/proxy.py` as a direct port of the desktop client's `proxy.py`, with `ServiceName` and `/service/<name>/` prefix. Port tests alongside.
- No desktop-client code changes yet. Old `/forwarding/` still works.

### Phase 3 — Service dispatcher on workspace_server

- Add `service_dispatcher.py` and wire into `server.py`:
  - Create shared `httpx.AsyncClient` in `_lifespan`.
  - Register `/service/{name}/__sw.js`, `/service/{name}/{path:path}` (HTTP + WS).
- Add `AgentManager.get_service_url` / `list_service_names`.
- Add `service_dispatcher_test.py` (integration).
- Manual verification: `curl http://localhost:8000/service/web/` inside a workspace reaches the web backend.

### Phase 4 — Frontend mode collapse

- Delete `getAccessMode()`, `getForwardingPrefix()`, hostname-sniffing branches in `getApplicationUrl` / `getTerminalUrl`.
- Rewrite iframe URLs to relative `/service/<name>/`.
- Collapse `layout-{mode}.json` to `layout.json`; drop the `mode` query param in `server.py::_layout_filename`.
- Delete `_inject_agent_id_meta_tag` / `getPrimaryAgentId()`.
- Manual verification: inside a dev workspace_server, dockview iframes load via `/service/<name>/`.

### Phase 5 — Desktop client subdomain routing

- Set `Domain=localhost` on session cookies in `cookie_manager.create_session_cookie`.
- Add host-header middleware in `app.py`:
  - Bare `localhost` → existing behavior.
  - `<agent-id>.localhost` → auth-gate + byte-level forward to the workspace_server URL from the resolver.
  - Unauth HTML → 302 to `/login?next=...`; others 403.
- Update landing-page links to `http://<agent-id>.localhost:8420/`.
- Update `test_desktop_client.py` and re-run.

### Phase 6 — Delete the old desktop-client forwarding

- Delete `apps/minds/.../desktop_client/proxy.py` + `proxy_test.py`.
- Delete `/forwarding/{agent_id}/...` routes and the old handlers (`_handle_proxy_http`, `_handle_proxy_websocket`, forwarding helpers).
- Simplify `backend_resolver.py` (keep per-service URLs only for Cloudflare sharing).
- Simplify `ssh_tunnel.py` to one tunnel per remote workspace.
- Update `api_v1.py`, `templates.py` accordingly.
- Re-run `test_desktop_client_e2e.py` (updated to new scheme). Ratchet bumps.

### Phase 7 — Vendor sync + acceptance

- Run `/release-minds` (or the equivalent) to push `vendor/mngr` in `forever-claude-template` to match.
- Confirm `test_meta_ratchets.py` for both repos passes.
- Full `just test-offload` on mngr; workspace-side tests via CI for `forever-claude-template`.

## Testing Strategy

### Unit

- `apps/minds_workspace_server/.../proxy_test.py` (ported from the desktop client):
  - Cookie path rewriting for all placement variants (no Path attr, Path=/, Path=/sub, Path already prefixed).
  - Absolute-path rewriting for href/src/action/formaction with single and double quotes, mixed case, protocol-relative untouched.
  - Service-worker / bootstrap / WS-shim generation: correct prefix baked in, correct cookie name set.
  - Backend loading HTML: fallback links include registered services and exclude the current one.

### Integration

- `apps/minds_workspace_server/.../service_dispatcher_test.py`:
  - Stub FastAPI backend on ephemeral port, known to `AgentManager`.
  - HTTP GET bootstrap flow (SW cookie → bootstrap HTML → subsequent request proxied).
  - SSE stream with `accept: text/event-stream` yields incrementally.
  - `Set-Cookie: foo=bar; Path=/` from backend rewritten to `Path=/service/stub/`.
  - HTML response has absolute paths rewritten and `<base>` + WS shim injected.
  - Unknown service HTML → loading page with fallback links; non-HTML → 502.
  - WebSocket upgrade proxied bidirectionally.

- `apps/minds/.../desktop_client/test_desktop_client.py`:
  - Host-header middleware dispatch for bare `localhost`, `<id>.localhost`, and unknown subdomain.
  - Session cookie issued with `Domain=localhost`.
  - Unauth HTML to subdomain → 302 to `/login?next=...`.
  - Auth'd request to `<id>.localhost` byte-forwards (headers and body intact).
  - `/forwarding/...` routes no longer exist (expect 404).

### Acceptance

- `apps/minds/test_desktop_client_e2e.py`:
  - Existing fixture boots the desktop client. Add a local `minds-workspace-server` subprocess with a known stub service in `applications.toml`.
  - `GET http://<agent-id>.localhost:PORT/` returns the workspace_server frontend HTML.
  - `GET http://<agent-id>.localhost:PORT/service/stub/` reaches the stub and the response carries a rewritten `Set-Cookie`.
  - `GET http://<agent-id>.localhost:PORT/` without the session cookie yields a 302 to `/login`.

### Edge cases and regressions to cover

- Services that register after the workspace_server starts (loading page during the gap).
- Services that deregister while in-flight requests are open (backend disappears mid-request → 502 with loading page for HTML).
- Cookies set at `Path=/` with other attributes (`Secure`, `HttpOnly`, `SameSite`) preserved after rewriting.
- HTML containing protocol-relative `//example.com/...` URLs — untouched by rewriting.
- WebSocket subprotocol forwarding (e.g. `tty` for ttyd) — preserved through the proxy.
- Remote workspaces: SSH tunnel creation and reuse work with exactly one port per workspace.

### Manual verification post-merge

- Electron full flow: login, pick workspace, interact with terminal + web iframe.
- Cloudflare-shared system_interface on a real domain: load in an external browser, confirm iframes render.
- Remote workspace (Modal / docker): confirm SSH tunnel is established once per workspace and everything still works.
- ttyd under `/service/terminal/`: confirm keyboard input and window resize behave.
- A third-party service (e.g. an ad-hoc FastAPI in `services.toml`) renders correctly as an iframe.

## Open Questions

(None. All decisions made during Q&A are final for this PR. Items like SW versioning, `*.localhost` fallbacks, rerouting non-system_interface Cloudflare hostnames through workspace_server, and per-service auth within a shared system_interface surface are deferred to future work if they become problems.)
