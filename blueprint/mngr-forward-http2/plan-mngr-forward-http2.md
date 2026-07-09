# HTTP/2 for the workspace UI (TLS + h2 at the `mngr forward` proxy)

## Overview

- **Problem.** The whole workspace UI is served over plain HTTP/1.1 from a single origin (`agent-<id>.localhost:<port>`), reached through the local `mngr forward` proxy. Chromium caps HTTP/1.1 at ~6 held-open connections per origin, so once a workspace has enough long-lived streams open (one SSE per open chat tab, plus any `/service/` app's own streams), the pool is exhausted and every further request — including the plain HTTP GETs that bootstrap a new terminal/app iframe — queues indefinitely. The UI hangs even though the backend is fine.
- **Fix.** Terminate TLS and negotiate HTTP/2 at the local `mngr forward` proxy. HTTP/2 multiplexes many concurrent streams over one connection, so the per-origin ceiling stops binding — regardless of how many tabs or streams are open. This is deliberately chosen over the multiplexed-WebSocket alternative: it removes the ceiling for *everything* on the origin (including third-party `/service/` apps' own streams), and it lives entirely in `mngr forward` rather than touching `system_interface`'s streaming model.
- **The proxy is already async.** The proxy app (`libs/mngr_forward/imbue/mngr_forward/server.py`) is already FastAPI/ASGI on asyncio (`async def` handlers, `run_in_executor`, `asyncio.gather`), served today by uvicorn (itself asyncio). Swapping uvicorn→hypercorn adds **no new async runtime** — the one visible change is a single `asyncio.run(...)` line in `cli.py`. The asyncio-removal migration targeted `system_interface` (the code coding-agents edit); the proxy is infra and was always async.
- **Why it's cheap here.** The only client of this origin is the minds Electron desktop app, so browser trust needs no OS trust store or CA install: the app accepts the proxy's self-signed cert for its loopback origins directly. The external-viewer path (Cloudflare-shared services) does not use this origin at all — it terminates TLS/h2 at the Cloudflare edge — so it is unaffected.
- **Shape of the change.** Add a `--use-http2` flag to `mngr forward` (default off). When set, the proxy generates a fresh ephemeral self-signed cert at startup and serves TLS + h2 via **hypercorn**; when unset it serves plain HTTP/1.1 via hypercorn exactly as today. There is a **single** `mngr forward` subprocess for the whole app, spawned by the **Python** minds backend (`apps/minds/imbue/minds/desktop_client/forward_cli.py`, called once at `apps/minds/imbue/minds/cli/run.py:403`); it serves every `agent-<id>.localhost` subdomain by host-header routing. minds hardcodes `--use-http2` on for that one subprocess — there is no per-workspace launch and no toggle.
- **Boundary.** Only the browser→proxy hop changes. The proxy→container hop (paramiko SSH tunnel) and the in-container `system_interface` werkzeug server stay plain HTTP/1.1 and are not touched. `SubagentView` streaming dedupe is explicitly out of scope.

## Expected behavior

- **The reported symptom is fixed.** With `--use-http2` on, a user can open well more than 6 streaming chat tabs in one workspace and then open a new terminal or app tab — it loads immediately. DevTools shows the workspace origin negotiated `h2` over a single connection, with no requests stuck in "Stalled".
- **No visible change otherwise.** Chat streaming, terminals, `/service/` app iframes, login, and the `/goto/<agent>` auth bridge all behave exactly as before; only the transport underneath changes. Activity badges and workspace state (the separate `/api/ws` socket) are unaffected.
- **The proxy origin flips to `https` consistently.** When h2/TLS is active, URLs the proxy and the minds client construct for the **proxy origin** become `https://`, its WebSocket URLs become `wss://`, and the proxy's `mngr_forward_session` cookie is marked `Secure`. When the flag is off, everything on the proxy stays `http`/`ws`/no-`Secure`, byte-for-byte as today.
- **The minds HTTP backend is untouched.** The minds bare-origin server (Home/Create/chrome/sidebar, its `minds_session` cookie, and the cookie-sync between session partitions) stays plain HTTP. The app deliberately runs a mixed transport — minds backend over `http://localhost:<minds-port>`, workspace content over `https://…:<proxy-port>` — which is fine (an http page loading/navigating to https is not mixed-content-blocked).
- **Trust is silent and scoped.** The Electron workspace session accepts the proxy's self-signed cert with no prompt, for loopback hosts (`localhost`, `*.localhost`, `127.0.0.1`) in the `persist:workspace-content` partition only. Every real `https` origin the app touches is unchanged — the override returns "defer to Chromium" for all non-loopback hosts, and external https (Claude auth, Cloudflare shares) opens in the system browser anyway (`isExternalUrl`, `main.js:204-226`), not in that partition.
- **Loopback probes still work.** The Python `httpx` readiness/recovery probes that dial the proxy on loopback keep working over TLS by disabling cert verification for those loopback-only clients (see Changes).
- **Failure is visible, not silently degraded.** With `--use-http2` set there is no fallback to plain HTTP. If the cert can't be generated or the TLS listener can't bind, `mngr forward` exits during startup; minds' `wait_for_listening` times out and the existing `run.py:451` "forward didn't come up" path surfaces it. `mngr forward` logs an explicit line naming TLS/h2 setup as the cause so the failure is diagnosable. Mid-flight handshake failures show as normal failed workspace loads in the existing loading/recovery UI. No new error UI is added.
- **Standalone `mngr forward` is unchanged by default.** A human running `mngr forward` without the flag (including `--open-browser` into a real browser) still gets plain HTTP/1.1 and no self-signed-cert friction. `--use-http2` is opt-in for clients that can trust the cert; minds is the one that always opts in.
- **WebSockets keep working, unchanged in shape.** Terminal (ttyd), the proto-agent log stream, and the `/api/ws` state socket become `wss` over the same TLS connection; Chromium negotiates `http/1.1` per-connection (via ALPN) for the WS upgrade, so they are not carried over h2 and are not otherwise modified. (They were never the constrained resource — WS has a much higher browser connection limit.)
- **Cloudflare-shared services are untouched.** An outside viewer still reaches a shared service at `https://<share-hostname>` via the Cloudflare edge → cloudflared → the service's own port; that path never involved this origin or the `mngr forward` proxy.

## Changes

### `mngr forward` proxy (`libs/mngr_forward`)

- **CLI flag.** Add `--use-http2` to `forward` (`cli.py`, default off), threaded into `create_forward_app` and the serve path so the app knows whether TLS is active. Regenerate CLI docs afterward (`uv run python scripts/make_cli_docs.py`) or the root `test_cli_docs_are_up_to_date` ratchet fails.
- **Ephemeral cert.** When the flag is on, generate a fresh self-signed cert at startup with `cryptography`: one cert, SANs `localhost` + `*.localhost` (both required — the wildcard does not cover the bare label; add `127.0.0.1` too so IP-host loopback clients verify). Generated in-memory, never written to disk, regenerated each startup.
- **Server swap uvicorn → hypercorn (asyncio worker).**
  - Replace the `uvicorn.Server(...).run(sockets=[listen_socket])` call (`cli.py:341-342`) with `asyncio.run(hypercorn.asyncio.serve(app, config))`.
  - **In-memory TLS:** subclass hypercorn `Config`, overriding `create_ssl_context()` to return an `ssl.SSLContext` built from the in-memory cert/key (ALPN `["h2", "http/1.1"]`), and `ssl_enabled` to `True`. No cert/key files touch disk. When the flag is off, plain HTTP/1.1 (no ssl context) — behavioral parity with today.
  - **Pre-bound socket handoff:** keep `_bind_listen_socket` (`cli.py:102-144`) as-is (binds, does not listen). Pass `bind=["fd://<fileno>"]`, using `os.dup(listen_socket.fileno())` for the fd so hypercorn (which closes the fd it wraps on shutdown) does not double-close the socket the `finally` block also closes. `asyncio.start_server` performs the `listen()`, so no bind race.
  - **Graceful shutdown:** pass `shutdown_trigger=None` so hypercorn installs its own SIGINT/SIGTERM handlers — matching uvicorn's SIGTERM→graceful behavior that minds' `terminate()` (`forward_cli.py:248`) relies on. Set `Config.graceful_timeout` ≈ 1s (matching today's `timeout_graceful_shutdown=1`). The existing `SIGHUP` handler (`cli.py:473`) is a different signal and is untouched; keep the `finally` cleanup (socket close, stream-manager stop, tunnel cleanup) after `asyncio.run` returns.
  - Add a clear startup log line naming TLS/h2 setup as the failure cause if cert generation or the TLS listener fails.
- **Scheme flips (conditional on TLS active).** Make these client-facing constructions `https`/`wss` when TLS is on, `http`/`ws` when off:
  - `cli.py:306-307` — login URL.
  - `server.py:184`, `server.py:186` — unauthenticated-subdomain redirect (`/` and `/goto/<agent>/`).
  - `server.py:835` — `/goto` → `_subdomain_auth` subdomain redirect.
  - **Do NOT change `server.py:696`** (`ws_backend = backend_url.replace("http://","ws://")…`) — that is the proxy→container hop and must stay `ws://`.
- **Cookie `Secure`.** Mark the proxy's own `mngr_forward_session` cookie `Secure` only when TLS is active, at both `set_cookie` sites: `_handle_subdomain_auth_bridge` (`server.py:541-547`) and `_handle_authenticate` (`server.py:771-778`). Do not touch any other cookie.
- **Backend-facing side unchanged.** The SSH `direct-tcpip` tunnel, the raw-TCP relay, and the httpx client dialing the in-container HTTP/1.1 backend are untouched.
- **Dependencies.** Add `hypercorn` and declare `cryptography` **explicitly** in `mngr_forward`'s `pyproject.toml` (do not rely on paramiko's transitive pull, so the wheel is self-contained). Drop `uvicorn` (used only in `cli.py` within this lib).

### minds Python backend (`apps/minds`)

- **Launch flag.** Add `--use-http2` to the argv built in `start_mngr_forward` (`forward_cli.py:619-633`) — one site, one flag, always on. (This is the single proxy for the whole app; there is no per-workspace launch.)
- **Loopback probes over TLS.** The Python `httpx` probes dial the proxy's front origin on loopback and must move to `https` with cert verification disabled for these loopback-only clients (per decision: `verify=False`; the probe targets `127.0.0.1` with a `Host: agent-<hex>.localhost` header, which cannot pass normal hostname verification anyway):
  - `make_workspace_probe_client` (`agent_creator.py:96-107`) → construct the `httpx.Client` with `verify=False`.
  - `probe_workspace_through_plugin` (`agent_creator.py:131-159`) → build `probe_url` as `https://127.0.0.1:<port>/` when h2 is on.
  - Confirms flow through `_await_system_interface_ready` (`workspace_recovery.py:139-159`) and `_wait_for_workspace_ready` (`agent_creator.py`).
- **Client-facing goto URL.** `_build_redirect_url` (`agent_creator.py:1996`) → `https://localhost:<port>/goto/<agent>/` when h2 is on.
- **minds HTTP backend untouched.** `minds_session` (host-only, `app.py:361-377`), the bare-origin login/authenticate flow, and the partition cookie-sync (`main.js:2472-2517`, over `http://`) are NOT changed — that server stays HTTP.

### minds Electron desktop client (`apps/minds/electron`)

- **Certificate override.** Register `session.fromPartition('persist:workspace-content').setCertificateVerifyProc((req, cb) => …)` (net-new; there is no existing cert hook): `cb(0)` (trust) when `req.hostname` is a loopback host (`localhost`, `*.localhost`, `127.0.0.1`), `cb(-3)` (defer to Chromium's default result) otherwise. The single shared `CONTENT_PARTITION` (`main.js:82`) covers all workspaces; the default session is not modified.
- **Proxy-origin scheme + cookie.** When h2 is on, build the proxy origin as `https://localhost:<mngr_forward_port>` in `handleMngrForwardStarted` (`main.js:2980-3007`) and mark the pre-set `mngr_forward_session` cookie `secure: true` (set it via a matching `https://` url). `workspaceUrlForAgent` (`main.js:231-241`) then builds `https` `/goto/` URLs automatically off `mngrForwardBaseUrl`. `backendBaseUrl` (the minds HTTP backend, `main.js:2763`) stays `http://`.

### Testing

- Unit-test the cert generation (SANs present) and the `Config.create_ssl_context()` override returns a context with the expected ALPN.
- Add TLS-path coverage for the scheme flips: existing `server_test.py:157` / `:285` assert `http://` on the flag-off default and stay valid; add flag-on variants asserting `https://` / `wss://` and the `Secure` cookie attribute.
- Cover the fd handoff (no double-close) and that `--use-http2` off is byte-for-byte the current behavior.

## Out of scope

- Any change to `system_interface` (its werkzeug server stays HTTP/1.1 behind the tunnel) or the minds HTTP backend origin.
- The multiplexed-WebSocket transcript fix and the `SubagentView` streaming-dedupe cleanup.
- HTTP/3 / QUIC, cert persistence/rotation, and making real (non-Electron) browsers trust the cert.
- A minds-side toggle / runtime HTTP fallback — `--use-http2` is hardcoded on; failure is surfaced, not silently degraded.
- Any change to the Cloudflare sharing path.
