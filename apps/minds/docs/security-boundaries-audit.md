# Security Boundaries Audit: Minds Electron App

Audit date: 2026-04-23

## Architecture summary

The minds desktop app uses a layered proxy architecture:

1. **Electron shell** (`electron/main.js`): Creates `BaseWindow` with multiple `WebContentsView` instances (chromeView, contentView, sidebarView, requestsPanelView). Manages window lifecycle and IPC.

2. **Desktop client** (FastAPI, `desktop_client/app.py`): Runs on `localhost:PORT`. Handles auth, agent discovery, and proxies `<agent-id>.localhost:PORT` subdomain requests to per-agent workspace servers.

3. **Workspace server** (`minds_workspace_server/`): One per agent. Multiplexes agent services under `/service/<name>/...` paths. Handles cookie path rewriting, Service Worker registration, and HTML rewriting.

4. **Agent services**: Individual HTTP servers (web UI, terminal, API, etc.) running inside each agent's container.

## Question 1: Can an agent read cookies set by another agent?

**Via JavaScript: NO.** Each agent runs on its own subdomain (`agent-A.localhost:PORT` vs `agent-B.localhost:PORT`). These are different origins, so the browser's same-origin policy prevents JavaScript on one agent's pages from accessing another agent's cookies, DOM, or storage.

**Via the agent's backend code: Previously YES, now FIXED.** This was the main finding of this audit.

The desktop client sets a `minds_session` cookie on each agent's subdomain via the auth bridge (`/goto/{agent_id}/` -> `/_subdomain_auth`). The cookie is signed with `itsdangerous.URLSafeTimedSerializer` using a single signing key, and the payload is always the string `"authenticated"` (see `cookie_manager.py:14`). Every agent's subdomain gets an independently-minted cookie, but they are all functionally identical -- any one of them would be valid on any other agent's subdomain if it could be obtained.

Previously, the desktop client proxy (`_forward_workspace_http`) forwarded all request headers except `host` to the workspace server, including the `minds_session` cookie. A malicious workspace server could have extracted the cookie and reused it against other agents.

**Fix applied:** The proxy now strips the `minds_session` cookie from the `Cookie` header before forwarding to the workspace server (see `app.py`, `_forward_workspace_http`). Auth is fully handled by the desktop client's middleware before the request reaches the proxy, so the workspace server does not need to see the session cookie. Additionally, the Electron content views now use a separate session partition (`persist:workspace-content`), isolating the content cookie jar from the chrome/sidebar views.

## Question 2: Can an agent access localStorage created by another agent?

**NO.** localStorage is origin-scoped. `agent-A.localhost:PORT` and `agent-B.localhost:PORT` are different origins, so they have completely separate localStorage, sessionStorage, and IndexedDB storage.

Note: Content views now use a separate Electron session partition (`persist:workspace-content`), while chrome, sidebar, and requests panel views use the default session. Even without this partition, web storage would still be scoped by origin per Chromium's standard behavior. The partition adds defense-in-depth by fully separating the content cookie jar from chrome-level cookies.

## Question 3: Can agents access cookies/localStorage used by the outer minds app?

**Cookies: NO.** The desktop client's session cookie is set on the bare `localhost:PORT` origin as a host-only cookie (no `Domain` attribute). The code explicitly documents why `Domain=localhost` is not used:

```python
# app.py:254-259
# Set a host-only session cookie on the bare origin. We do NOT try to
# share the cookie across `<agent-id>.localhost` subdomains via
# ``Domain=localhost`` -- both curl and Chromium treat ``localhost`` as
# a public suffix and refuse to send such cookies to subdomains. Each
# subdomain gets its own cookie set on first visit, minted via the
# ``/goto/{agent_id}/`` auth-bridge redirect below.
```

The bare-origin `minds_session` cookie is never sent to `agent-X.localhost` subdomains. Agents cannot read it.

**localStorage: NO.** The desktop client's chrome UI pages load from `localhost:PORT` (e.g., `/_chrome`, `/_chrome/sidebar`). Agent content loads from `agent-X.localhost:PORT`. Different origins = separate localStorage.

**Electron IPC: NO.** The preload script (which exposes `window.minds` IPC bridge) is only loaded in chromeView, sidebarView, and requestsPanelView. The contentView (where agent pages render) is created without a preload script (`main.js:217-222`), so agent pages cannot access Electron IPC.

## Detailed isolation mechanisms

### Cookie scoping

| Layer | Mechanism | Status |
|-------|-----------|--------|
| Between agents (browser-side) | Origin isolation: different subdomains | Secure |
| Between agents (backend-side) | Session cookie stripped by proxy before forwarding | Secure (fixed) |
| Between services within an agent | Cookie `Path` rewriting (`/service/<name>/`) | Secure |
| Between agents and desktop client | Host-only cookies, no `Domain=localhost` | Secure |
| Service Worker cookies | Scoped to `/service/<name>/` path | Secure |

### Web storage scoping

| Storage type | Scoping mechanism | Status |
|-------------|-------------------|--------|
| localStorage | Origin isolation | Secure |
| sessionStorage | Origin isolation | Secure |
| IndexedDB | Origin isolation | Secure |
| Service Worker cache | Service Worker scope (`/service/<name>/`) | Secure |

### Electron-level isolation

| Component | Current state | Risk |
|-----------|--------------|------|
| WebContentsView session | Content views use `persist:workspace-content` partition; chrome/sidebar use default session | Secure -- cookie jars separated between content and chrome |
| Content Security Policy | Not explicitly set by desktop client | Low -- agents control their own CSP |
| contextIsolation | Enabled on all views | Secure |
| nodeIntegration | Disabled on all views | Secure |
| Preload script | Only on chrome/sidebar/requests views, NOT on contentView | Secure |

## Options considered for fixing the session cookie fungibility

### Option A: Strip the auth cookie in the proxy -- IMPLEMENTED

The desktop client proxy (`_forward_workspace_http`) strips the `minds_session` cookie from the `Cookie` header before forwarding to the workspace server. Auth is fully handled by the desktop client's middleware before the request reaches the proxy -- the workspace server does not need to see or validate the session cookie.

Implementation: In `_forward_workspace_http`, the `Cookie` header is parsed, the `minds_session` cookie is removed, and the remaining cookies are forwarded. Non-session cookies (e.g. service-specific cookies) are preserved.

Pros:
- Minimal code change
- No breaking changes to workspace server behavior (services' own cookies still flow through)
- Defense-in-depth: even if Electron session sharing were misconfigured, the cookie would never reach agent code

Cons:
- If any workspace server feature ever needs to verify auth (currently none do), it would need an alternative mechanism

### Option B: Per-agent session cookies (not implemented)

Make each subdomain's session cookie cryptographically bound to that specific agent ID. Change the cookie payload from `"authenticated"` to something like `f"authenticated:{agent_id}"`, and verify the agent ID when checking cookies.

Pros:
- Cookies are no longer fungible -- extracting agent A's cookie doesn't help with agent B
- No changes to the proxy layer

Cons:
- More complex than Option A
- The extracted cookie would still be usable against the same agent (less concerning)
- Does not prevent the workspace server from seeing the cookie at all

### Option C (variant): Content session partitioning -- IMPLEMENTED

Rather than per-agent partitions (which would require complex cookie re-synchronization), a single shared content partition (`persist:workspace-content`) is used for all content views. Chrome, sidebar, and requests panel views continue to use the default Electron session. A cookie sync mechanism copies `minds_session` cookies from the content partition to the default session so that chrome-level auth checks work.

This separates the content cookie jar from the chrome cookie jar, adding defense-in-depth. Agents remain origin-isolated within the content partition via standard Chromium same-origin policy.

Pros:
- Separates content and chrome cookie jars
- Simpler than per-agent partitions -- no need to track which partition each agent uses
- Cookie sync keeps chrome views authenticated

Cons:
- Agents share a single content partition (origin isolation still applies within it)
- Adds complexity in cookie synchronization between partitions

### Option D: Combine A + C (not implemented as originally described)

The implemented approach combines Option A with a variant of Option C -- cookie stripping in the proxy plus a shared content partition (rather than per-agent partitions).

## What was implemented

**Option A (cookie stripping) and a variant of Option C (shared content partition) are both implemented.** Option A directly prevents the session cookie from reaching workspace servers. The content partition provides defense-in-depth by separating content and chrome cookie jars at the Electron level.
