# Minds App: WebContentsView Refactor + Workspace Sidebar

## Overview

- The minds Electron app currently uses a single BrowserWindow for everything -- title bar is injected via JS/CSS on every navigation, proxied workspace content and desktop-client pages share the same web contents, and the service worker has to carve out `/auth/` routes manually because it can't distinguish workspace requests from desktop-client requests
- This refactoring replaces the single BrowserWindow with a BaseWindow containing three WebContentsViews: a persistent chrome view (title bar), a content view (all page content), and a toggleable sidebar view (workspace switcher overlay)
- The chrome is served by the Python backend at `/_chrome` (Jinja2 template), not injected via JS. Browser (non-Electron) users get the same chrome HTML but with an iframe instead of a WebContentsView, keeping one implementation for both modes
- This isolates the service worker properly (removing the `/auth/` exception), eliminates the `body { padding-top }` CSS hack, and simplifies the title bar lifecycle (rendered once, never re-injected)
- The sidebar provides workspace switching without navigating away from the current page -- clicking a workspace drives the content view to that workspace's default server

## Expected Behavior

### Electron app

- On startup, shell.html shows in the window during backend startup (unchanged)
- Once the backend is running, the chrome view loads `/_chrome` and the content view loads `/` (the landing page)
- The title bar (home, sidebar toggle, back/forward, title, user menu, window controls) is persistent and never reloads across navigation
- Clicking the sidebar toggle button (near the home button, upper left) shows the sidebar view sliding in from the left over the content area, with a drop shadow. Clicking the toggle again or selecting a workspace closes it
- The sidebar lists workspace names, fed by SSE from `/_chrome/events`. It shows nothing until the user is authenticated, then populates as workspaces are discovered
- Clicking a workspace in the sidebar navigates the content view to `/forwarding/{agent_id}/` and closes the sidebar
- Back/forward buttons operate on the content view's navigation history
- The page title in the chrome updates to reflect the content view's current page title
- Window resize updates all three views' bounds programmatically
- If the backend crashes, the chrome view loads shell.html (error/retry screen) and the content and sidebar views are removed

### Browser access

- Navigating to the server URL in a regular browser loads the `/_chrome` page
- The chrome renders the same title bar (minus window controls) with an iframe for content instead of a WebContentsView
- The sidebar is CSS-positioned within the chrome page (no third view needed), toggled via JS
- The sidebar is fed by the same SSE endpoint
- Back/forward buttons use `iframe.contentWindow.history`
- The browser info bar (agent name/host/application wrapper) is removed -- replaced by the chrome

### Service worker

- The service worker continues to operate under `/forwarding/{agent_id}/{server_name}/` scope
- The `/auth/` exception (`if (url.pathname.startsWith('/auth/')) return;`) is removed because auth pages load in the content view (or iframe), which is a separate web contents from the chrome. The chrome's auth status fetch (`/auth/api/status`) runs in the chrome view's own web contents, outside any service worker scope

### Authentication

- The `/_chrome` route is unauthenticated -- the chrome page renders for all users
- The sidebar shows an empty state for unauthenticated users
- After authentication (via the content view loading `/auth/login`), the SSE stream from `/_chrome/events` begins delivering workspace data
- The user menu in the chrome fetches auth status from `/auth/api/status` (same-origin, no service worker interference since it runs in the chrome view)

## Implementation Plan

### Python backend changes

#### New route: `/_chrome` (GET, unauthenticated)
- Serves the chrome Jinja2 template
- Template receives: `platform` (from User-Agent or explicit param), `is_authenticated` (bool), `initial_workspaces` (list of `{id, name}` dicts if authenticated)
- Returns the full chrome HTML including title bar markup, sidebar markup (hidden by default), CSS, and JS
- File: `apps/minds/imbue/minds/desktop_client/app.py` -- add route handler `_handle_chrome_page`
- File: `apps/minds/imbue/minds/desktop_client/templates.py` -- add `_CHROME_TEMPLATE` and `render_chrome_page()`, plus `_SIDEBAR_TEMPLATE` and `render_sidebar_page()`

#### New route: `/_chrome/sidebar` (GET, unauthenticated)
- Serves the sidebar-only HTML (workspace list + SSE subscription JS)
- Used by the Electron sidebar WebContentsView
- File: `apps/minds/imbue/minds/desktop_client/app.py` -- add route handler `_handle_chrome_sidebar`
- File: `apps/minds/imbue/minds/desktop_client/templates.py` -- add `render_sidebar_page()`

#### New route: `/_chrome/events` (GET, SSE)
- Server-Sent Events endpoint
- On connection: if authenticated, sends current workspace list as JSON array; if not, sends `{"type": "auth_required"}`
- On workspace change (added/removed/renamed): sends updated full workspace list
- On auth state change: sends `{"type": "auth_status", "signed_in": true/false, ...}`
- Uses `BackendResolverInterface.list_known_workspace_ids()` and `get_workspace_name()` for data
- File: `apps/minds/imbue/minds/desktop_client/app.py` -- add SSE handler `_handle_chrome_events`

#### Modify proxy handler (`_handle_proxy_http`)
- Remove the `is_electron_client()` branching that wraps content in `generate_browser_info_bar_html()`
- Remove the `_embed=1` query parameter handling
- All clients now receive raw proxied content (the chrome/iframe wrapper is handled at the chrome level)

#### Modify service worker generation (`proxy.py`)
- Remove `if (url.pathname.startsWith('/auth/')) return;` from `generate_service_worker_js()`

#### Remove functions
- `generate_browser_info_bar_html()` from `proxy.py`
- `is_electron_client()` from `proxy.py`

#### Register new routes
- `app.get("/_chrome")(_handle_chrome_page)`
- `app.get("/_chrome/sidebar")(_handle_chrome_sidebar)`
- `app.get("/_chrome/events")(_handle_chrome_events)`

### Electron changes

#### `main.js` -- major rewrite

**Replace BrowserWindow with BaseWindow + WebContentsViews:**
- `createWindow()`: create a `BaseWindow` (same window options minus `webPreferences`). Create three `WebContentsView`s:
  - `chromeView`: preload = `preload.js`, loads `/_chrome` from backend
  - `contentView`: no preload, loads `/` from backend
  - `sidebarView`: no preload, loads `/_chrome/sidebar` from backend, initially not added to window
- Add `chromeView` and `contentView` to `win.contentView` via `addChildView()`
- Set bounds: `chromeView` at `{x:0, y:0, width:W, height:TITLEBAR_HEIGHT}`, `contentView` at `{x:0, y:TITLEBAR_HEIGHT, width:W, height:H-TITLEBAR_HEIGHT}`
- On macOS, adjust chromeView bounds to account for traffic light position

**Remove title bar injection:**
- Delete `TITLEBAR_CSS`, `TITLEBAR_CSS_MAC`, `TITLEBAR_HTML`, `TITLEBAR_JS` constants
- Remove the `dom-ready` event handler that injects CSS/JS
- Remove the `will-navigate` file:// guard (chrome never navigates)

**Add resize handler:**
- Listen for `resize` event on BaseWindow
- Update `chromeView`, `contentView`, and `sidebarView` (if shown) bounds

**Add content view event forwarding:**
- Listen to `contentView.webContents` events:
  - `page-title-updated`: send `content-title-changed` IPC to chromeView
  - `did-navigate` / `did-navigate-in-page`: send `content-url-changed` IPC to chromeView

**Add sidebar toggle IPC:**
- `toggle-sidebar`: if sidebarView is not a child of the window, add it and set bounds `{x:0, y:TITLEBAR_HEIGHT, width:260, height:H-TITLEBAR_HEIGHT}`; if it is, remove it
- `navigate-content`: call `contentView.webContents.loadURL(url)` then remove sidebarView if shown

**Modify startup sequence:**
- `runStartupSequence()`: load shell.html in chromeView for the loading phase
- Once backend is ready: load `/_chrome` in chromeView, load the login URL in contentView (or `/` after auth)
- On backend crash: load shell.html in chromeView, remove contentView and sidebarView

**Modify IPC handlers:**
- `go-home`: navigate contentView to `backendBaseUrl + '/'`
- `content-go-back`: `contentView.webContents.goBack()`
- `content-go-forward`: `contentView.webContents.goForward()`
- Remove `open-external` handler

#### `preload.js` -- expand bridge

Add to `contextBridge.exposeInMainWorld('minds', { ... })`:
- `navigateContent: (url) => ipcRenderer.send('navigate-content', url)`
- `contentGoBack: () => ipcRenderer.send('content-go-back')`
- `contentGoForward: () => ipcRenderer.send('content-go-forward')`
- `toggleSidebar: () => ipcRenderer.send('toggle-sidebar')`
- `onContentTitleChange: (cb) => ipcRenderer.on('content-title-changed', (_e, title) => cb(title))`
- `onContentURLChange: (cb) => ipcRenderer.on('content-url-changed', (_e, url) => cb(url))`

Remove:
- `openExternal`

#### Delete files
- `electron/titlebar.html` (legacy, unused)

### Chrome template (`/_chrome`)

**HTML structure:**
```
<div id="minds-titlebar">
  <div class="minds-nav">
    <button id="sidebar-toggle">  <!-- panel icon -->
    <button id="home-btn">        <!-- home icon -->
    <button id="back-btn">        <!-- back chevron -->
    <button id="forward-btn">     <!-- forward chevron -->
  </div>
  <span id="page-title">Minds</span>
  <div class="minds-user-area">
    <button id="user-btn">Login</button>
    <div id="user-dropdown">
      <button id="settings-btn">Settings</button>
      <button id="signout-btn">Sign out</button>
    </div>
  </div>
  <!-- Window controls (non-macOS only) -->
  <div class="minds-wc">
    <button id="min-btn">...</button>
    <button id="max-btn">...</button>
    <button id="close-btn">...</button>
  </div>
</div>

<!-- Sidebar (browser mode only, hidden by default) -->
<div id="sidebar-panel" class="sidebar-hidden">
  <div id="sidebar-workspaces"></div>
</div>

<!-- Content area (browser mode only) -->
<iframe id="content-frame" src="/"></iframe>
```

**CSS:**
- Title bar: fixed, height 38px, same dark theme as current (`#1e293b`)
- macOS: `padding-left: 72px` for traffic lights, hide `.minds-wc`
- Sidebar panel: `position: fixed; left: 0; top: 38px; width: 260px; height: calc(100% - 38px); background: #f3f2ef; box-shadow: 4px 0 12px rgba(0,0,0,0.15); transform: translateX(-100%); transition: transform 200ms ease-in-out;`
- Sidebar visible: `transform: translateX(0);`
- Content iframe: `position: fixed; left: 0; top: 38px; width: 100%; height: calc(100% - 38px); border: none;`

**JS (adapter pattern):**
```javascript
const isElectron = !!window.minds;

// Navigation
function navigateContent(url) {
  if (isElectron) window.minds.navigateContent(url);
  else document.getElementById('content-frame').src = url;
}
function goBack() {
  if (isElectron) window.minds.contentGoBack();
  else document.getElementById('content-frame').contentWindow.history.back();
}
function goForward() {
  if (isElectron) window.minds.contentGoForward();
  else document.getElementById('content-frame').contentWindow.history.forward();
}

// Sidebar toggle
function toggleSidebar() {
  if (isElectron) window.minds.toggleSidebar();
  else document.getElementById('sidebar-panel').classList.toggle('sidebar-hidden');
}

// Title tracking
if (isElectron) {
  window.minds.onContentTitleChange(title => {
    document.getElementById('page-title').textContent = title || 'Minds';
  });
} else {
  // Poll iframe title
  setInterval(() => {
    try {
      var t = document.getElementById('content-frame').contentDocument.title;
      document.getElementById('page-title').textContent = t || 'Minds';
    } catch(e) {}
  }, 500);
}

// SSE for workspace list
const evtSource = new EventSource('/_chrome/events');
evtSource.onmessage = function(event) {
  const data = JSON.parse(event.data);
  if (data.type === 'workspaces') renderWorkspaces(data.workspaces);
  if (data.type === 'auth_status') updateAuthUI(data);
};

// Auth status
fetch('/auth/api/status').then(r => r.json()).then(updateAuthUI);

// Window controls (Electron only)
if (isElectron) {
  document.getElementById('min-btn').onclick = () => window.minds.minimize();
  document.getElementById('max-btn').onclick = () => window.minds.maximize();
  document.getElementById('close-btn').onclick = () => window.minds.close();
}
```

### Sidebar template (`/_chrome/sidebar`)

- Standalone HTML page loaded in the Electron sidebar WebContentsView
- Contains the workspace list and SSE subscription (same `/_chrome/events` endpoint)
- Clicking a workspace sends IPC via the preload bridge: `window.minds.navigateContent('/forwarding/{id}/')`
- Minimal styling matching the chrome theme
- Note: the sidebar WebContentsView needs its own preload to expose `navigateContent` -- reuse the same `preload.js`

## Implementation Phases

### Phase 1: Backend routes

- Add `/_chrome`, `/_chrome/sidebar`, and `/_chrome/events` routes to the FastAPI app
- Add Jinja2 templates for chrome and sidebar pages
- Add SSE handler that streams workspace list updates
- Remove `generate_browser_info_bar_html()`, `is_electron_client()`, and the `_embed=1` branching from the proxy handler
- Remove the `/auth/` exception from `generate_service_worker_js()`
- **Result**: Backend serves chrome and sidebar pages; proxy handler simplified. Existing Electron app still works (title bar injection is still present, just unused by the new chrome route)

### Phase 2: Electron restructure

- Replace `BrowserWindow` with `BaseWindow` + three `WebContentsView`s in `main.js`
- Remove title bar CSS/JS injection code, `will-navigate` guard, `titlebar.html`
- Expand `preload.js` with new IPC channels
- Add resize handler, content event forwarding, sidebar toggle IPC
- Update startup sequence to load chrome and content views from backend
- Update crash recovery to replace views with shell.html
- **Result**: Electron app uses the new architecture. Chrome is persistent, sidebar works, content is isolated

### Phase 3: Cleanup and browser mode

- Verify browser access works with the chrome + iframe architecture
- Remove `openExternal` from preload bridge and main.js IPC handler
- Remove `titlebar.html` legacy file
- Test that service worker works without the `/auth/` exception
- Test that `body { padding-top }` is no longer needed (content view is properly bounded)
- **Result**: Both Electron and browser modes work with the unified chrome. All deprecated code removed

## Testing Strategy

### Unit tests

- `generate_service_worker_js()`: verify the `/auth/` exception line is absent
- `render_chrome_page()`: verify template renders with correct platform info, auth state, workspace data
- `render_sidebar_page()`: verify template renders workspace list
- Verify `generate_browser_info_bar_html()` and `is_electron_client()` are no longer referenced

### Integration tests

- `/_chrome` route returns HTML with correct title bar markup, sidebar markup, and iframe for non-Electron clients
- `/_chrome/sidebar` route returns sidebar HTML with SSE subscription JS
- `/_chrome/events` SSE endpoint:
  - Returns `auth_required` for unauthenticated clients
  - Returns workspace list for authenticated clients
  - Pushes updates when workspaces change
- Proxy handler returns raw content without info bar wrapper for all User-Agents
- Service worker allows `/auth/` requests to pass through without the explicit exception (they're outside SW scope)

### Manual verification

- Electron app: launch, verify chrome renders once, navigate between workspaces, verify title updates, verify back/forward works, verify sidebar toggle shows/hides workspace list, verify selecting a workspace navigates content and closes sidebar
- Browser: navigate to server URL, verify chrome + iframe renders, verify sidebar toggle works, verify workspace switching works
- Resize: verify views resize correctly on window resize
- Auth flow: verify unauthenticated state shows empty sidebar, login populates sidebar
- Crash recovery: kill backend, verify error screen appears, retry works

### Edge cases

- Window resize while sidebar is open: sidebar bounds update correctly
- Rapid sidebar toggle: no view leak or stale state
- Backend not yet ready: chrome shows shell.html loading state
- Multiple workspaces: sidebar lists all, each navigates correctly
- Single workspace: landing page still works as expected

## Open Questions

- Should the sidebar WebContentsView reuse the same `preload.js` as the chrome view? This would give the sidebar access to all IPC channels (navigateContent, window controls, etc.). The alternative is a separate minimal preload, but that adds a file to maintain
- The SSE endpoint `/_chrome/events` needs a mechanism to detect workspace changes. The `BackendResolverInterface` currently provides snapshot methods (`list_known_workspace_ids`). It may need a callback or polling mechanism to push changes. The `MngrStreamManager` already watches for agent events -- this could be extended to notify the SSE handler
- Should the sidebar show workspace status (running/stopped/creating) in addition to names? This was deferred but would be straightforward to add via the SSE data
- The `generate_backend_loading_html()` function contains links to convention servers (terminal, agent). With the new architecture, should these links still work? They navigate to `/forwarding/{agent_id}/{server}/` which would load in the content view -- this should work as-is
