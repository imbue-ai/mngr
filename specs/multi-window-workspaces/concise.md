# Multi-Window Workspaces

## Overview

* Today the minds Electron app is single-window: one `BaseWindow` with one `contentView` that the sidebar navigates between workspaces (`/forwarding/{agent_id}/...`). To view two workspaces side-by-side, users must leave the app entirely.
* This spec adds multi-window support: each workspace can live in its own window, with at most one window per workspace enforced across the whole app.
* Opening a new window is always an explicit user action (right-click "Open in new window" or a hover icon in the sidebar). A plain sidebar click still navigates the current window — unless the target workspace is already open somewhere, in which case that other window is focused instead.
* The backend is unchanged. All windows share the same backend session, same cookie-based auth, and the same set of backend routes. This is purely an Electron-layer refactor: module-level window state in `main.js` becomes a per-window registry, IPC handlers become sender-aware, and the backend's per-window views (content, sidebar, requests panel, chrome title bar) are instantiated once per window.
* Shutdown becomes window-count-driven: closing a single window just closes that window; the backend only shuts down when the last window closes.

## Expected Behavior

### Window identity and uniqueness

* A window is "on workspace X" whenever its content view's URL starts with `/forwarding/{X}/`. A window on `/` or any non-workspace route does not count as a workspace window.
* There is no "home" window concept. A window is just a window; its role follows its current URL. A workspace window becomes a non-workspace window as soon as the user navigates it away (e.g. via the home button); a non-workspace window becomes a workspace window when it navigates into `/forwarding/X/`.
* At most one window can be on any given workspace at any time. Two windows both on workspace X is never a valid state. Uniqueness is enforced at the Electron level via a `will-navigate` listener on every content view: any navigation to `/forwarding/{X}/...` that targets a workspace already open in a different window is cancelled (`event.preventDefault()`) and the existing window is focused instead. This catches every navigation path — sidebar IPC, landing-page row clicks, in-page anchors, `window.location` assignments — not just the sidebar.

### Sidebar clicks

* Clicking a workspace entry in the sidebar:
  * If some window is already on that workspace → focus that window. The clicked window is untouched.
  * Otherwise → navigate the current window to `/forwarding/{X}/` (works whether the current window was on `/`, on another workspace, or anywhere else). The previous workspace of this window, if any, is no longer open in any window.
* The sidebar itself is unchanged visually. No indicator is shown for "currently open elsewhere" or "this window's current workspace."

### Explicit "Open in new window"

* Two affordances, both in the sidebar, both doing the same thing:
  * Right-click on a sidebar workspace entry → context menu with a single item: "Open in new window."
  * A small "open in new window" icon next to each sidebar entry, revealed on hover.
* Both actions: if the target workspace is already open in another window, focus that window (uniqueness still wins); otherwise create a new window and load `/forwarding/{X}/` in it.
* If the current window is itself on the target workspace, both affordances are hidden/disabled for that entry (you can't open a window that already exists).
* The new window uses Electron default position/size. No cascade, no remembering per-workspace positions.

### Window titles

* OS-level window title (dock, taskbar, alt-tab):
  * Workspace window: `{workspace-name} — Minds`, where `{workspace-name}` is the same `w.name || w.id` the sidebar uses. Until the name has loaded, the title is `Minds`; it updates in place when the workspace list is received.
  * Non-workspace window: `Minds`.
* In-window chrome title bar (the page title element inside the title bar): mirrors the OS title. The main process pushes a `window-title-changed` IPC to each window's chrome view whenever the computed title changes (content-view navigation or workspace-list update) and on chrome-view `did-finish-load`. `document.title` from the content view is ignored in Electron mode. (In plain-browser mode the chrome template still polls `document.title` from the iframe.)

### Notifications and `auth_required`

* Workspace-specific notifications (notification event's URL starts with `/forwarding/{X}/`): clicking the notification focuses the existing window for workspace X if one is open; otherwise opens a new window. The window navigates to the full URL from the event, preserving any deep path.
* `auth_required` backend events, and notifications whose URL is non-workspace (e.g. `/auth/login`, `/accounts`): focus the most recently focused window and navigate it to that URL. No new window is created.
* "Most recently focused window" is tracked via the OS-level `focus` event on each `BaseWindow` — the window that most recently gained OS focus (by click, alt-tab, or programmatic focus) wins.

### Requests panel

* Any window can independently open the right-side requests panel from its own title bar, identical to today.
* Clicking a request in the requests panel follows the workspace-notification rule: focus the target workspace's window if open, otherwise open a new window pointed at that request's URL.

### Shutdown

* Closing a single window (via the close button, cmd+W, or `window-close` IPC) destroys only that window and its associated views (content, sidebar, requests panel). The backend keeps running.
* When the last window closes, the backend is shut down (SIGTERM, then SIGKILL after 5s — same as today) and the app quits.
* `cmd+Q` / `ctrl+Q` triggers the full quit path explicitly: close all windows, then shut down the backend.
* macOS keeps the same cross-platform behavior — the app quits when the last window closes (no dock-icon-alive state).

### Backend crash / startup error

* If the backend exits unexpectedly, the error screen is shown in every open window simultaneously (each window's chrome view switches to the error UI, all content/sidebar/requests-panel views are torn down).
* "Retry" from any window restarts the backend once. On success, every window reloads: chrome view returns to `/_chrome` and content view returns to its pre-error URL.

### Single-instance lock and second-launch

* The single-instance lock stays. A second `minds` launch focuses the most recently focused window of the running instance (same MRU signal as above).

### Session restoration

* On quit, the app records the set of open windows and each window's current URL (including deep paths within a workspace).
* On next launch, after auth completes, reopen one window per recorded URL, each navigated to the exact URL it had at quit. Windows use default position/size (no position restoration).
* Destroyed workspaces are silently skipped: for each recorded URL that targets `/forwarding/{X}/`, if workspace X no longer exists in the current agent list, drop that URL from the restoration set without opening a window or surfacing any error.
* If no windows remain after filtering (e.g. fresh install, or every recorded workspace was destroyed), the app opens a single window at the landing URL, same as today.

### Keyboard shortcuts

* cmd+W / ctrl+W: close the current window. Does NOT shut down the backend unless it was the last window.
* cmd+Q / ctrl+Q: quit the app — close all windows, shut down the backend.
* cmd+N / ctrl+N: open a new window pointed at the home page (`/`). No workspace uniqueness check (the home page is not a workspace window).
* Existing DevTools shortcut (cmd+opt+I / ctrl+shift+C) continues to toggle DevTools for the focused window's content view.

### Opening a new home window

There are three entry points, all of which open a fresh window on the backend's home page (`/`) — the same landing page a new install would see. None of them accept a workspace parameter; for workspace-specific "new window" see `Explicit "Open in new window"` above.

* **Application menu → File → New Window** (macOS): a custom application menu is installed that keeps the standard app/edit/window submenus and adds a `File` menu containing `New Window` (bound to cmd+N) and `Close Window` (cmd+W).
* **Dock context menu → New Window** (macOS): `app.dock.setMenu(...)` installs a single-item menu that opens a new home window.
* **Keyboard shortcut cmd+N / ctrl+N**: works on every platform. On macOS it's bound via the File-menu accelerator; on Windows/Linux the application menu is hidden, so the shortcut is registered per-window via `before-input-event` alongside cmd+W / cmd+Q.

The new window uses Electron default position/size, loads `/_chrome` in its chrome view, and loads `/` in its content view. It appears in the MRU list as soon as it receives focus.

## Changes

### Electron main process (`apps/minds/electron/main.js`)

* Replace module-level window state (`mainWindow`, `chromeView`, `contentView`, `sidebarView`, `requestsPanelView`) with a per-window bundle registry. Each bundle owns its own `BaseWindow` plus its four `WebContentsView`s (chrome, content, sidebar, requests panel) and tracks its current content URL and current workspace ID (derived from URL).
* Add a helper to parse the workspace ID from a URL (`/forwarding/{agentId}/...`) and update a window's recorded workspace ID on content view `did-navigate` / `did-navigate-in-page`.
* Add a central `openOrFocusWorkspace(agentId, url)` routine used by sidebar clicks, notifications, and requests-panel clicks. It enforces uniqueness (focus if open, else create new window) and sets up the window's initial URL.
* Add `navigateCurrentWindowToWorkspace(sourceWindow, agentId)` for the plain-click path: if workspace is already open elsewhere, focus that; else navigate the source window's content view.
* Rewrite all IPC handlers (`go-home`, `navigate-content`, `content-go-back`, `content-go-forward`, `toggle-sidebar`, `toggle-requests-panel`, `open-requests-panel`, `retry`, `window-minimize`, `window-maximize`, `window-close`) to resolve the target window from the IPC event's sender (`event.sender.getOwnerBrowserWindow()` or equivalent) instead of the single `mainWindow`.
* Update `navigate-content` to branch: if the URL is a workspace URL (`/forwarding/{X}/...`) and another window is already on workspace X, focus that window without navigating the sender; otherwise navigate the sender's content view.
* Add a new IPC channel `open-workspace-in-new-window` invoked by the sidebar's right-click menu and hover-icon clicks; routes through `openOrFocusWorkspace`.
* Update the OS window title to `{workspace-name} — Minds` on workspace windows and `Minds` elsewhere. Track the latest sidebar workspace list (received via a new IPC broadcast from the sidebar's SSE) per-window or app-wide, and update window titles when the list changes or when the content view navigates.
* Push the same computed title into each window's chrome view via a new `window-title-changed` IPC (exposed on the preload bridge as `onWindowTitleChange`) so the in-window title bar mirrors the OS title. The chrome template uses this in Electron mode instead of `document.title`.
* Centralize the `/_chrome/events` SSE subscription in the main process (`runChromeSSELoop`, using `net.request` with `useSessionCookies: true`). Broadcast each event to every window's chrome and sidebar view via a new `chrome-event` IPC; templates subscribe via `window.minds.onChromeEvent` and fall back to a direct `EventSource` only in plain-browser mode. This avoids exhausting Chromium's 6-connection-per-host cap once you open a couple of windows (each previously opened its own SSE connection, which starved subsequent `/_chrome/sidebar`, `/_chrome/requests-panel`, and in-app navigations for seconds at a time).
* Lazy-create the sidebar/requests-panel `WebContentsView` per window and toggle with `setVisible` instead of destroy-and-recreate on every click. Destroying spawned a fresh render process and preload load per toggle, which in rapid-click scenarios queued many seconds of latency.
* On `BaseWindow` `close`, explicitly call `webContents.close()` on every child view (chrome/content/sidebar/requests-panel). `BaseWindow` doesn't guarantee destruction of its child `WebContentsView` render processes; leaking them across create/close cycles eventually starves new ones.
* On the content view, attach `will-prevent-unload` → `event.preventDefault()` so pages that install a `beforeunload` handler (e.g. workspace pages with open websockets) don't stall navigations waiting for a non-existent confirmation dialog.
* Update `window-all-closed` handler: only call `shutdown()` + `app.quit()` when the last window is gone. Individual window close events just tear down that window's bundle.
* Add a `cmd+Q` / `ctrl+Q` accelerator to trigger the full-quit path (close all bundles + shutdown).
* Add a `cmd+W` / `ctrl+W` accelerator to close the focused window's bundle only.
* Add a `cmd+N` / `ctrl+N` accelerator (and a `openHomeInNewWindow()` helper) that opens a fresh bundle pointed at `/`.
* On macOS, install a custom application `Menu` that keeps the standard app/edit/window roles and adds a `File` menu containing `New Window` (cmd+N) and `Close Window` (cmd+W). When `MINDS_HIDE_MENU=1` the menu is suppressed and shortcuts fall back to per-window `before-input-event` handling.
* On macOS, call `app.dock.setMenu(Menu.buildFromTemplate([{ label: 'New Window', click: openHomeInNewWindow }]))` so the dock icon's right-click menu offers the same action.
* Maintain an app-wide MRU list of open windows: push to front on each `BaseWindow` `focus` event, drop on `closed`. The single-instance `second-instance` handler and the no-workspace code paths (`auth_required`, non-workspace notification clicks) resolve the target window from this list.
* Rework the backend-crash / error flow: `showError` fans out to every open window's chrome view. `retry` IPC handler, once it succeeds, reloads every window to its recorded pre-error URL.
* Update the notification `onClick` callback to route through `openOrFocusWorkspace` for workspace URLs, and through "focus most recent + navigate" for non-workspace URLs.
* Update the `auth_required` handler similarly (no workspace involved — always uses the MRU window).
* Write session state to disk on `before-quit`: a JSON list of content-view URLs, one per open window. Read it on `whenReady` and, after the backend is ready, open one window per recorded URL.

### Sidebar (desktop-client backend + preload bridge)

* Extend the sidebar HTML template (`_SIDEBAR_TEMPLATE` in `apps/minds/imbue/minds/desktop_client/templates.py`) so each workspace row supports:
  * A right-click that triggers a native Electron context menu (not an in-page styled `<div>`). The sidebar HTML listens for the DOM `contextmenu` event on a workspace row and sends an IPC message to the main process with the row's agent ID and click coordinates (relative to the sidebar view). The main process builds a native menu via Electron's `Menu.buildFromTemplate([{ label: 'Open in new window', click: ... }])` and calls `menu.popup({ window, x, y })` on the sidebar window. This keeps visual fidelity (native OS menu appearance + behavior) while letting the sidebar still drive the content.
  * A hover-only "open in new window" icon on the right of each row. Clicking it sends the same IPC as the context menu's "Open in new window" item.
  * Suppression of both affordances for the entry matching the sender window's current workspace. The sidebar needs to know its owning window's current workspace — the main process pushes that information in via an IPC broadcast whenever the window's content view navigates.
* The preload bridge (`apps/minds/electron/preload.js`) gains:
  * `openWorkspaceInNewWindow(agentId)` — fires the `open-workspace-in-new-window` IPC.
  * `showWorkspaceContextMenu(agentId, x, y)` — fires a `show-workspace-context-menu` IPC so the main process can pop up a native menu.
  * `onCurrentWorkspaceChanged(callback)` — receives updates on this window's current workspace (used to suppress self-affordances).
* The sidebar informs the main process of its rendered workspace list (ids + display names) via a new IPC message, so the main process can resolve workspace names for OS window titles without duplicating the SSE connection.
* Auto-close behavior: the sender's sidebar closes after every sidebar action -- plain workspace click (whether it navigates the current window or focuses another), hover-icon "Open in new window" click, and native context-menu "Open in new window" click. The rationale is that once the user has picked a workspace the sidebar has served its purpose; leaving it open just occludes content.

### Backend (`apps/minds/imbue/minds/desktop_client/`)

* No behavior changes. The backend continues to serve `/_chrome`, `/_chrome/sidebar`, `/_chrome/requests-panel`, and all `/forwarding/{agent_id}/...` routes.
* Sanity check: confirm that multiple Electron windows hitting the same backend on the same session cookie is a supported pattern for the sharing/auth endpoints (`auth_required`, SuperTokens token refresh). No changes expected.

### Dev-mode parity

* Dev mode (`pnpm start`, `uv run --package minds ...`) inherits all behavior automatically — no separate code paths in `backend.js`.

## Open Questions

* None outstanding.
