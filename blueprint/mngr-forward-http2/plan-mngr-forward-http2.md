# HTTP/2 for the workspace UI (TLS + h2 at the `mngr forward` proxy)

## Overview

- **Problem.** The whole workspace UI is served over plain HTTP/1.1 from a single origin (`agent-<id>.localhost:<port>`), reached through the local `mngr forward` proxy. Chromium caps HTTP/1.1 at ~6 held-open connections per origin, so once a workspace has enough long-lived streams open (one SSE per open chat tab, plus any `/service/` app's own streams), the pool is exhausted and every further request — including the plain HTTP GETs that bootstrap a new terminal/app iframe — queues indefinitely. The UI hangs even though the backend is fine.
- **Fix.** Terminate TLS and negotiate HTTP/2 at the local `mngr forward` proxy. HTTP/2 multiplexes many concurrent streams over one connection, so the per-origin ceiling stops binding — regardless of how many tabs or streams are open. This is deliberately chosen over the multiplexed-WebSocket alternative: it removes the ceiling for *everything* on the origin (including third-party `/service/` apps' own streams), and it lives entirely in `mngr forward` rather than touching `system_interface`'s streaming model.
- **Why it's cheap here.** The only client of this origin is the minds Electron desktop app, so browser trust needs no OS trust store or CA install: the app accepts the proxy's self-signed cert for its loopback origins directly. The external-viewer path (Cloudflare-shared services) does not use this origin at all — it terminates TLS/h2 at the Cloudflare edge — so it is unaffected.
- **Shape of the change.** Add a `--use-http2` flag to `mngr forward`. When set, the proxy generates a fresh ephemeral self-signed cert at startup and serves TLS + h2; when unset it serves plain HTTP/1.1 exactly as today. The desktop client passes the flag for every workspace it opens. The proxy's server is switched from uvicorn (HTTP/1.1-only) to hypercorn (h2-capable, otherwise at parity).
- **Boundary.** Only the browser→proxy hop changes. The proxy→container hop (paramiko SSH tunnel) and the in-container `system_interface` werkzeug server stay plain HTTP/1.1 and are not touched. `SubagentView` streaming dedupe is explicitly out of scope.

## Expected behavior

- **The reported symptom is fixed.** With `--use-http2` on, a user can open well more than 6 streaming chat tabs in one workspace and then open a new terminal or app tab — it loads immediately. DevTools shows the workspace origin negotiated `h2` over a single connection, with no requests stuck in "Stalled".
- **No visible change otherwise.** Chat streaming, terminals, `/service/` app iframes, login, and the `/goto/<agent>` auth bridge all behave exactly as before; only the transport underneath changes. Activity badges and workspace state (the separate `/api/ws` socket) are unaffected.
- **The origin flips to `https` consistently.** When h2/TLS is active, URLs the proxy and desktop client construct become `https://`, WebSocket URLs become `wss://`, and the proxy session cookie + the workspace auth cookie are marked `Secure`. When the flag is off, everything stays `http`/`ws`/no-`Secure`, byte-for-byte as today.
- **Trust is silent and scoped.** The Electron workspace session accepts the proxy's self-signed cert with no prompt, for loopback origins (`localhost`, `*.localhost`) in that session partition only. Validation for every real `https` origin the app loads (Claude auth pages, Cloudflare share URLs, any external content) is unchanged — the accept override never widens to them.
- **Failure is visible, not silently degraded.** With `--use-http2` set there is no fallback to plain HTTP. If TLS/h2 can't be established, the workspace fails to load with a surfaced error rather than quietly reverting.
- **Standalone `mngr forward` is unchanged by default.** A human running `mngr forward` without the flag (including `--open-browser` into a real browser) still gets plain HTTP/1.1 and no self-signed-cert friction. `--use-http2` is opt-in for clients that can trust the cert; the desktop app is the one that always opts in.
- **WebSockets keep working, unchanged in shape.** Terminal (ttyd), the proto-agent log stream, and the `/api/ws` state socket become `wss` over the same TLS connection; they are not carried over h2 and are not otherwise modified. (They were never the constrained resource — WS has a much higher browser connection limit.)
- **Cloudflare-shared services are untouched.** An outside viewer still reaches a shared service at `https://<share-hostname>` via the Cloudflare edge → cloudflared → the service's own port; that path never involved this origin or the `mngr forward` proxy.

## Changes

### `mngr forward` proxy (`libs/mngr_forward`)

- Add a `--use-http2` CLI flag (default off), threaded through to the serve path.
- Add ephemeral self-signed cert generation at startup when the flag is on: one cert with SANs covering `localhost` and `*.localhost`, generated in-memory, never persisted, regenerated each startup. (`cryptography` is already available in the minds dependency set.)
- Replace the uvicorn server with hypercorn as the ASGI server for the proxy:
  - Plain HTTP/1.1 when `--use-http2` is off (behavioral parity with today).
  - TLS + h2 when on, using the generated cert/key and ALPN advertising `h2, http/1.1`.
  - Preserve the existing pre-bound-socket handoff (uvicorn's `sockets=[socket]` becomes hypercorn's `bind=["fd://<fileno>"]`) so there is no bind race, and preserve graceful shutdown.
- Make the scheme of every URL the proxy constructs (login URL, `/goto` → `_subdomain_auth` redirect, backend/WS rewrites) conditional on whether TLS is active: `https`/`wss` when on, `http`/`ws` when off.
- Mark the proxy's own session cookie `Secure` when serving over TLS.
- Leave the backend-facing side entirely alone: the SSH `direct-tcpip` tunnel, the raw-TCP relay, and the httpx client that dials the in-container HTTP/1.1 backend are unchanged.

### minds Electron desktop client (`apps/minds`)

- Launch `mngr forward` with `--use-http2` for every workspace (default-on).
- Register a certificate-verification override on the workspace session partition that accepts any cert whose host is a loopback origin (`localhost` / `*.localhost`), and defers to normal validation for all other hosts. Scope it to that partition so no real `https` origin is affected.
- Flip the client-constructed workspace/login/goto URLs and cookie origins to `https://` (and `wss://` where applicable) to match the proxy, including marking the pre-set proxy session cookie and the workspace auth cookie `Secure`.

### Dependencies

- Add `hypercorn` (and, if not already resolvable to `mngr_forward` at runtime, `cryptography`) to `mngr_forward`'s dependencies; drop the `uvicorn` dependency from the proxy if nothing else in the lib needs it.

### Out of scope

- Any change to `system_interface` (its werkzeug server stays HTTP/1.1 behind the tunnel).
- The multiplexed-WebSocket transcript fix and the `SubagentView` streaming-dedupe cleanup.
- HTTP/3 / QUIC, cert persistence/rotation, and making real (non-Electron) browsers trust the cert.
- Any change to the Cloudflare sharing path.
