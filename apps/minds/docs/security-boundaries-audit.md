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

**Via the agent's backend code: YES -- the session cookie is fungible across agents.** This is the main finding of this audit.

Here's the issue: the desktop client sets a `minds_session` cookie on each agent's subdomain via the auth bridge (`/goto/{agent_id}/` -> `/_subdomain_auth`). The cookie is signed with `itsdangerous.URLSafeTimedSerializer` using a single signing key, and the payload is always the string `"authenticated"` (see `cookie_manager.py:14`). Every agent's subdomain gets an independently-minted cookie, but they are all functionally identical -- any one of them is valid on any other agent's subdomain.

When the desktop client proxies an HTTP request from the browser to a workspace server (`app.py:483-553`, `_forward_workspace_http`), it forwards all request headers except `host`:

```python
headers = dict(request.headers)
headers.pop("host", None)
body = await request.body()
```

This means the workspace server receives the `minds_session` cookie in the `Cookie` header. The workspace server is controlled by the agent (it runs inside the agent's container from template code). A malicious or compromised agent could extract the `minds_session` cookie from any incoming request and use it to make authenticated HTTP requests to another agent's subdomain (`agent-B.localhost:PORT`), bypassing the desktop client's auth check.

**Severity: Medium.** Requires the agent's backend code to be actively malicious. In practice, agents are created from user-chosen template repos, so this is only exploitable if a template is backdoored or an agent is compromised. However, the architecture should not rely on agent code being trusted -- the desktop client is supposed to be the trust boundary.

## Question 2: Can an agent access localStorage created by another agent?

**NO.** localStorage is origin-scoped. `agent-A.localhost:PORT` and `agent-B.localhost:PORT` are different origins, so they have completely separate localStorage, sessionStorage, and IndexedDB storage.

Note: All `WebContentsView` instances in the Electron app share the **default session** (no `partition` is specified in `webPreferences` -- see `main.js:217-222`). However, even within a shared Electron session, web storage is still scoped by origin per Chromium's standard behavior. So while the cookie jar is technically shared at the Chromium layer, storage APIs remain isolated per-origin.

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
| Between agents (backend-side) | Session cookie is fungible | **Vulnerable** |
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
| WebContentsView session | All share default session (no `partition`) | Low -- origin isolation still applies |
| Content Security Policy | Not explicitly set by desktop client | Low -- agents control their own CSP |
| contextIsolation | Enabled on all views | Secure |
| nodeIntegration | Disabled on all views | Secure |
| Preload script | Only on chrome/sidebar/requests views, NOT on contentView | Secure |

## Options for fixing the session cookie fungibility

### Option A: Strip the auth cookie in the proxy (recommended)

The desktop client proxy (`_forward_workspace_http`) should strip the `minds_session` cookie from the `Cookie` header before forwarding to the workspace server. Auth is already fully handled by the desktop client's middleware before the request reaches the proxy -- the workspace server does not need to see or validate the session cookie.

Implementation: In `_forward_workspace_http`, parse the `Cookie` header, remove the `minds_session` cookie, and forward the rest. This is a small, surgical change.

Pros:
- Minimal code change
- No breaking changes to workspace server behavior (services' own cookies still flow through)
- Defense-in-depth: even if Electron session sharing were misconfigured, the cookie would never reach agent code

Cons:
- If any workspace server feature ever needs to verify auth (currently none do), it would need an alternative mechanism

### Option B: Per-agent session cookies

Make each subdomain's session cookie cryptographically bound to that specific agent ID. Change the cookie payload from `"authenticated"` to something like `f"authenticated:{agent_id}"`, and verify the agent ID when checking cookies.

Pros:
- Cookies are no longer fungible -- extracting agent A's cookie doesn't help with agent B
- No changes to the proxy layer

Cons:
- More complex than Option A
- The extracted cookie would still be usable against the same agent (less concerning)
- Does not prevent the workspace server from seeing the cookie at all

### Option C: Electron session partitioning

Use per-agent `partition` in the contentView's `webPreferences`:

```javascript
const contentView = new WebContentsView({
    webPreferences: {
        partition: `persist:${agentId}`,
        contextIsolation: true,
        nodeIntegration: false,
    },
});
```

Pros:
- Gives each agent a completely separate cookie jar, cache, and storage
- Strongest isolation at the Electron layer

Cons:
- Requires the auth bridge to work with per-partition sessions (cookies set in one partition aren't visible in another)
- More complex window management (need to track which partition each content view uses)
- May break session restore if agent IDs change

### Option D: Combine A + C (strongest)

Strip auth cookies in the proxy (Option A) AND partition Electron sessions (Option C). This provides defense-in-depth at both the proxy layer and the Electron layer.

## Recommendation

**Option A (strip auth cookies in the proxy) should be implemented first.** It is the simplest change, directly addresses the fungibility issue, and has no architectural side effects. Option C (session partitioning) is worth considering as a follow-up for defense-in-depth, but is not strictly necessary given that origin isolation already handles JavaScript-level storage separation.
