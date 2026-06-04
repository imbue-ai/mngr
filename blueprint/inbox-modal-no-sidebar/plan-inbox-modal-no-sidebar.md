# Plan: requests inbox modal + sidebar removal

## Refined prompt

I want to accomplish two things here: (1) get rid of the sidebar surface — the home page seem to cover exactly the same info (we should open in new window button there) and (2) i want to move the requests surface into the modal surface (combining them) instead of showing a panel on the right, let's have the panel appear in a modal — that way nothing moves and it should be easy to click out into the workspace that triggered it. we will combine the permission info that's currently appearing in a modal with this panel — inbox style. Here is a rough sketch: https://www.figma.com/design/1p1nrkoHia3OxahQOkmHh3/Minds-Early-IA-Explorations?node-id=488-4383&t=D0X7nv6rcuGNEKhz-11 (don't worry about the colors and such let's use existing patterns in the app instead)

* Remove the sidebar surface entirely: drop the `Sidebar.jinja` page, the `/_chrome/sidebar` route, the `sidebar-toggle` button in the chrome titlebar, and the `Sidebar` WebContentsView in Electron. The home page already covers the same info.
* On the home page, add an always-visible "open in new window" icon button per workspace row, matching the existing pattern of the restart and settings icon buttons in `Landing.jinja`. Reuse the existing SVG from `sidebar.js:36-39` and the existing `window.minds.openWorkspaceInNewWindow(agentId)` IPC.
* Keep the chrome titlebar's `requests-toggle` button (and its badge), but it now opens the new inbox **modal** instead of the right-side panel. Drop the right-side panel WebContentsView and its 320px content offset.
* The new inbox uses the same Electron modal overlay pattern as today's permission dialog (transparent overlay above the workspace, dim backdrop, click-out + Escape to close). Clicking out stays on whatever's behind it — same as today.
* Inbox modal panel: **80% of the window width with a max-width cap, full height (top to bottom of the window — modal overlay view extends to y=0, covering the titlebar while open), Gmail-style two-pane** — list of pending requests on the left, selected request's permission UI on the right.
* After grant/deny: stay in the modal showing the inbox list; auto-advance the detail pane to the next pending request; close the modal only when the inbox empties.
* Treat all requests uniformly through the inbox modal — including `is_user_requested` requests (currently auto-navigated to a content-view page); they now auto-open the modal with that request selected.
* Default selection on open: auto-open path selects the newly-arrived request; manual open (via the chrome button) shows a "Select a request" empty detail pane until the user clicks one.
* Detail pane carries a "Go to workspace" action that closes the modal and navigates the content view to the request's workspace.
* Empty-inbox state: clicking the chrome `requests-toggle` still opens the modal, which shows an empty state plus the existing `auto_open_requests_panel` checkbox.
* Sidebar carryover: drop the sidebar's account-grouping; port the sidebar's per-workspace right-click context menu to the home-page rows.
* Browser-mode parity: render the modal in-chrome via CSS overlay (inline HTML inside `Chrome.jinja`), same UX in browser and Electron.

## Overview

* Two unrelated changes ship together because they both reshape the chrome's left/right edges.
* **Sidebar deletion.** The home page (`Landing.jinja`) and the sidebar (`Sidebar.jinja`) both render `_build_workspace_list()` (`app.py:1695`). The sidebar is redundant; deleting it removes a WebContentsView, a route, a Jinja page, a static JS file, the chrome's hamburger toggle, and Electron's `toggle-sidebar` IPC. The home page picks up the sidebar's one unique affordance: an "open in new window" icon button per row, plus the right-click context menu.
* **Right-side panel → modal inbox.** Today's `/_chrome/requests-panel` lives in a 320px-wide WebContentsView pinned to the right; opening it shrinks the content view. We replace it with a full-window modal that overlays the workspace (and the titlebar) without moving anything, so closing the modal returns the user to exactly where they were — which is usually the workspace that triggered the request.
* **Inbox + permission detail merge.** The permission dialog (`PermissionsDialog.jinja`) and the requests panel are two windows onto the same data. The new modal collapses them into a single Gmail-style two-pane surface: the request list on the left, the selected request's permission UI on the right.
* The Electron modal-overlay machinery (`openModal` / `closeModal` in `main.js:723-790`) already exists and is used by today's permission dialog. We extend it: bounds widen to `(0, 0, width, height)` so the modal covers the titlebar; the URL it loads is the new inbox page, not a per-request page.
* SSE plumbing for pending-requests is unchanged on the server (`_handle_chrome_events` keeps emitting `{type: "requests", count, request_ids, auto_open}`). The chrome's reaction changes: instead of toggling a panel WebContentsView, it opens / refreshes the modal.

## Expected behavior

### Sidebar removal

* The hamburger `sidebar-toggle` button at the left of the chrome titlebar disappears. The "Home" button is the sole entry point to the workspace list.
* No matter what was previously visible (open or closed sidebar), the layout reflows: the content area extends to the left edge of the window minus the chrome inset.
* Browser mode: the inline `#sidebar-panel` div in `Chrome.jinja` and its renderer in `chrome.js` are gone; the chrome no longer renders a workspace list.
* Electron mode: the sidebar `WebContentsView` is no longer created; `window.minds.toggleSidebar()` is removed. Any external caller that previously called it is a no-op (we audit and delete).
* The home page (`/`) gains a new always-visible icon button on each workspace row, between the existing restart and settings icons. Same visual style (`bg-transparent border border-transparent rounded-md cursor-pointer p-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600`). Clicking it calls `window.minds.openWorkspaceInNewWindow(agentId)` (Electron) or no-ops gracefully (browser). The button uses the existing external-link SVG from `sidebar.js:36-39`.
* The home page gains a right-click context menu per workspace row, with the same actions and IPC (`window.minds.showWorkspaceContextMenu(agentId, x, y)`) as the sidebar's current context menu.
* Account-grouping headers (`PRIVATE` / per-email) that lived in the sidebar are **not** ported to the home page — the home page keeps its flat list.

### Requests inbox modal

* The chrome's `requests-toggle` button still shows a badge with the pending count (existing SSE-driven behavior).
* Clicking the button opens the inbox modal. Clicking it again closes the modal. The button stays enabled (and pressable) even when the count is zero — opening an empty inbox is a valid action because the auto-open checkbox lives inside it.
* On the auto-open path (a new request arrives while the modal is closed and `auto_open_requests_panel` is true), the modal opens and the detail pane is pre-populated with the newly-arrived request.
* On the manual-open path (user clicks the chrome button), the detail pane starts empty: a centered "Select a request from the list" placeholder.
* The modal panel is **80% of the window width**, capped at e.g. `max-w-[1280px]`, centered horizontally. Vertically it spans the full window — top edge at y=0, bottom edge at y=window-height. The Electron titlebar view is hidden / covered while the modal is open; closing the modal restores it.
* Click outside the panel (dim backdrop) or press Escape to close. Closing the modal stays on whatever was behind it (current workspace, home page, etc.) — no implicit navigation.
* The list pane shows one row per pending request, sorted most-recent-first (matches `get_pending_requests()`). Each row shows `<kind>: <workspace-name>` (top line) and the handler's `display_name_for_event(req)` (secondary line). The selected row is visually highlighted.
* The detail pane renders the same form that today's permission dialog renders — same handler (`handler.render_request_page`), same `POST /requests/{id}/grant` and `/requests/{id}/deny` actions, same approve/deny/manual-credentials flow. Form submit is XHR (already today), and on `outcome === "GRANTED"` the detail pane advances to the next request rather than closing the modal.
* When `outcome === "GRANTED"` from grant/deny:
  * The SSE `"requests"` event arrives with the resolved request removed.
  * The list rerenders without the resolved row.
  * The detail pane auto-advances to the next pending request (top of the list, since it's sorted most-recent-first). If the inbox is now empty, the modal closes automatically.
* The modal's footer carries the existing `auto_open_requests_panel` checkbox, persisting via `POST /_chrome/requests-auto-open`.
* The detail pane carries a "Go to workspace" action (link or button styled to match existing patterns). Clicking it closes the modal and calls `window.minds.navigateContent(mngrForwardOrigin + "/goto/<agent-id>/")` (Electron) or `window.top.location = ".../goto/<agent-id>/"` (browser).
* `is_user_requested` requests no longer auto-navigate the content view to `/requests/<id>`. Instead they participate in the inbox like everything else: they fire the SSE `"requests"` payload, and (because `auto_open_requests_panel` defaults true) the modal opens with that request selected.
* Browser-mode parity: the modal is inline HTML in `Chrome.jinja`, hidden by default, shown via a CSS class toggle when the requests-toggle is clicked. SSE drives the same list/detail rerenders, fetched from new fragment endpoints (see Implementation plan). Escape, backdrop click, and the chrome button all close it.

## Implementation plan

### Backend (`apps/minds/imbue/minds/desktop_client/app.py`)

* **Remove sidebar route and renderer.**
  * Delete `_handle_sidebar_page` (the route handler) and the `/_chrome/sidebar` registration.
  * Delete `render_sidebar_page` in `templates.py` (currently at ~`templates.py:970-982`).
  * Delete `templates/pages/Sidebar.jinja`.
  * Delete `static/sidebar.js`.
* **Replace requests-panel route with inbox modal page.**
  * Rename `_handle_requests_panel` → `_handle_requests_inbox` and change its rendered URL/route from `/_chrome/requests-panel` to `/_chrome/requests-inbox`.
  * Replace its hand-built HTML with a Jinja template `templates/pages/RequestsInbox.jinja` that lays out the modal: backdrop + two-pane card + footer with the auto-open checkbox.
  * Add two new fragment endpoints used by both Electron and browser modes to keep list+detail rendering on the server:
    * `GET /_chrome/requests-inbox/list` → renders the list pane (a list of selectable cards, each carrying `data-event-id` and `data-agent-id`).
    * `GET /_chrome/requests-inbox/detail/{event_id}` → renders the detail pane (essentially today's `_handle_request_page` body without the outer `Base` wrapper).
  * Keep `POST /_chrome/requests-auto-open` exactly as is.
  * Keep `GET /requests/{event_id}`, `POST /requests/{event_id}/grant`, `POST /requests/{event_id}/deny` as is, so existing form actions continue to work. The grant/deny JSON response shape is unchanged (`outcome`, `message`, `set_credentials_example`).
* **Refactor the `RequestEventHandler` interface to support fragment rendering.**
  * Add `render_request_fragment(req_event, backend_resolver, mngr_forward_origin) -> Response` to the abstract base, returning the body only (no `<html>`/`<body>`/`Base` wrapper). Default implementation may delegate to `render_request_page` and extract the body, but the cleanest approach is to refactor each handler to put its body content into a partial template that both `render_request_page` and `render_request_fragment` reuse.
  * Update existing handlers (`latchkey/handlers/predefined.py`, `latchkey/handlers/file_sharing.py`, and the permissions handler) to expose their inner form fragment.
  * The new `/_chrome/requests-inbox/detail/{event_id}` endpoint calls `render_request_fragment` instead of `render_request_page`.
* **`_build_requests_payload` (existing).**
  * Unchanged. Already returns `{count, request_ids}`. The `auto_open` is still bundled with the SSE event.
* **Remove `is_user_requested` auto-navigation.**
  * The flag's runtime effect is moved from "navigate the content view to `/requests/<id>`" to "treat as if `auto_open_requests_panel` is true for this event" (i.e. force-open the inbox modal even if the user has disabled auto-open). The SSE `"requests"` payload gains a `force_open_event_id` field when the most-recent unresolved pending request was user-requested; the chrome's SSE handler opens the modal with that event selected.
  * The `is_user_requested` flag itself stays on the event type (other writers continue to set it; minds' history visualization can still surface it). Only the consumer behavior changes.

### Chrome view (`apps/minds/imbue/minds/desktop_client/templates/pages/Chrome.jinja` + `static/chrome.js`)

* **Remove sidebar markup and JS.**
  * Delete the `sidebar-toggle` button in the chrome titlebar (`Chrome.jinja:35-38`) and any associated CSS / margin offsets.
  * Delete the `#sidebar-panel` div (`Chrome.jinja:84-89`) and its CSS sibling rules.
  * In `chrome.js`, delete `renderWorkspaces`, the workspace SSE handling, and the sidebar toggle/select logic (currently ~`chrome.js:228-277` and surrounding).
* **Add the inbox modal markup in browser mode.**
  * Add a hidden `#requests-inbox-modal` div in `Chrome.jinja`, with the two-pane structure (list pane container + detail pane container) and a hidden-by-default state.
  * In Electron, this div stays hidden (Electron uses the modal overlay view), but its existence is harmless.
* **Wire the requests-toggle button.**
  * The handler at `chrome.js:200-202` becomes a unified function: in Electron, call `window.minds.toggleRequestsModal()`; in browser, toggle the inline `#requests-inbox-modal` div, fetch the list fragment via `/_chrome/requests-inbox/list`, and render it.
  * Browser mode subscribes to SSE `"requests"` events and re-fetches the list fragment when the payload changes.
  * Browser mode click-handlers on the list rows fetch the detail fragment from `/_chrome/requests-inbox/detail/<event-id>` and inject it into the detail pane container.
  * Browser mode listens for backdrop click and `Escape` to close.
  * Browser mode also honors `force_open_event_id` from the SSE payload: opens the modal and selects that event.
* **Drop the cross-iframe `minds:open-request-modal` postMessage handler** at `chrome.js:214-225`. Workspaces no longer drive request opens via postMessage; the inbox modal is opened via the same SSE channel as everything else. (If a workspace needs to *cause* an immediate inbox-open for its current request, it sets `is_user_requested=true` on the event, which produces `force_open_event_id` server-side.)

### Landing page (`apps/minds/imbue/minds/desktop_client/templates/pages/Landing.jinja`)

* **Add the "open in new window" icon button per row.**
  * Insert a new button before the existing restart and settings icon buttons (`Landing.jinja:413-430`). Same Tailwind classes. SVG copied verbatim from `sidebar.js:36-39`.
  * `onclick="event.stopPropagation(); window.landingOpenNewWindowClick(this)"` and a new `window.landingOpenNewWindowClick` function in the page's inline `<script>` that reads `data-agent-id` from the row and calls `window.minds.openWorkspaceInNewWindow(agentId)`.
* **Add right-click context menu.**
  * Add a `contextmenu` listener on the workspace-row container that calls `window.minds.showWorkspaceContextMenu(agentId, e.clientX, e.clientY)` if available, else no-ops.
  * The IPC handler in `main.js` (already wired for the sidebar's context menu) is reused without modification.

### Electron main (`apps/minds/electron/main.js`)

* **Delete sidebar machinery.**
  * Remove the `sidebar` field from each bundle (around `main.js:394`), `createSidebarView`, `openSidebar`, `closeSidebar`, `toggleSidebar`, and the `'toggle-sidebar'` IPC at `main.js:1913`.
  * Remove sidebar bounds calculation from `updateBundleBounds` (`main.js:215`); the sidebar no longer participates in layout.
* **Delete requests-panel WebContentsView.**
  * Remove `requestsPanel` field, `createRequestsPanelView`, `openRequestsPanel`, `closeRequestsPanel`, `toggleRequestsPanel`, the panel-reload debounce / timer, and the `'toggle-requests-panel'` + `'open-requests-panel'` IPCs.
  * Remove the `rightOffset = 320` adjustment to the content view in `updateBundleBounds`. The content view always spans the full width below the titlebar.
* **Repurpose the modal overlay view for the inbox.**
  * The existing `openModal(bundle, url)` already supports any URL. Change the chrome's wiring (and the `'navigate-to-request'` IPC) to load `/_chrome/requests-inbox?event_id=<id>` when there's a target event, or `/_chrome/requests-inbox` when manually opening.
  * Add a `'toggle-requests-modal'` IPC: opens or closes the modal, loading `/_chrome/requests-inbox` (no event).
  * Add a `'requests-modal-open'` IPC: opens the modal and includes a target event id (used by the auto-open / `force_open_event_id` path).
  * Extend the modal overlay view's bounds so it covers the titlebar: change the modal entry in `updateBundleBounds` from `(0, 38, width, height - 38)` to `(0, 0, width, height)`. The titlebar `WebContentsView` is still present; the modal sits above it in z-order while visible (the modal view is re-added to the bundle's view stack each time it opens, raising it to top, per the existing pattern).
  * When closing the modal, the modal overlay view's `setVisible(false)` already restores the titlebar's interactivity — no extra work needed beyond bounds.
* **Replace the cross-iframe `open-request-modal` handler.**
  * The old handler at `main.js:1952` (triggered by content-view postMessage `minds:open-request-modal`) loaded `/requests/<id>` directly. Either:
    1. Remove it entirely, since `is_user_requested` is now the canonical way for workspaces to summon the inbox (server-driven via SSE), or
    2. Repoint it to `/_chrome/requests-inbox?event_id=<id>` so existing content-view code that calls it still works.
  * Pick (2) for backwards compatibility during the transition; remove the postMessage relay (`content-relay-preload.js`) in a follow-up.
* **Bundle state.**
  * Remove `sidebarVisible` and `requestsPanelVisible` from each bundle's state; add `requestsModalVisible` (or just rely on the existing `modalVisible` flag, since the modal overlay view is shared with the permission modal — but note the inbox and permission dialog are now collapsed into one surface, so there's only ever one modal anyway).

### Preload (`apps/minds/electron/preload.js`)

* Remove `toggleSidebar` and `toggleRequestsPanel` from the `window.minds` bridge.
* Add `toggleRequestsModal` / `openRequestsInbox(eventId)` (whichever shape ends up cleaner).
* Keep `openWorkspaceInNewWindow`, `navigateContent`, `closeModal`, `showWorkspaceContextMenu` unchanged.

### CSS / tokens

* Audit `tokens.css` and `Sidebar.jinja`-adjacent styles; delete any sidebar-only rules.
* Add the inbox modal's styles (or use Tailwind classes inline) — list pane, detail pane, selected-row highlight, "Go to workspace" link.

### Permission dialog (`templates/PermissionsDialog.jinja`)

* The Jinja template stays as a wrapper for `GET /requests/<id>` (direct-load fallback, useful for tests + email-style deep links).
* The handler's body content moves to a shared partial that both the dialog page and the inbox detail-pane endpoint render. The wrapper page only adds the modal backdrop and the surrounding `Base`.

### Data flow audit

* Sidebar tests: any pytest or e2e test that exercises `/_chrome/sidebar`, the sidebar JS, or the toggle IPC.
* Requests-panel tests: any pytest or e2e test that exercises `/_chrome/requests-panel`.
* `is_user_requested` tests: behavior changes — they auto-open the modal, not the content view.

## Implementation phases

Each phase produces a working (but potentially incomplete) system.

### Phase 1 — Sidebar removal (mechanical)

* Delete the sidebar route, template, JS, Electron view, IPCs, and the `sidebar-toggle` button.
* Adjust `updateBundleBounds` so the content view fills the freed left space.
* Add the "open in new window" icon button + right-click context menu to `Landing.jinja`.
* Update / delete any tests that reference the sidebar.
* Ship-ready state: requests panel still works (untouched); home page covers all sidebar functionality; layout is correct.

### Phase 2 — Handler fragment refactor

* Add `render_request_fragment` to `RequestEventHandler` and implement it for all existing handlers.
* Verify that `render_request_page` still returns identical output to today (the wrapper just calls the fragment from inside `Base`).
* No user-visible behavior change. Ratchet check and unit tests stay green.

### Phase 3 — Backend inbox endpoints

* Add `templates/pages/RequestsInbox.jinja` (two-pane layout) and the new endpoints:
  * `GET /_chrome/requests-inbox` (full page)
  * `GET /_chrome/requests-inbox/list` (list fragment)
  * `GET /_chrome/requests-inbox/detail/{event_id}` (detail fragment)
* Add the `force_open_event_id` field to the SSE `"requests"` payload, derived from the most-recent pending `is_user_requested` event.
* Remove the old `_handle_requests_panel` and its route; the right-side panel is now unreachable.
* Ship-ready state: the chrome's requests-toggle is currently broken (it expects the old IPC), but the new endpoints work standalone. Browser-mode users navigating directly to `/_chrome/requests-inbox` get a working page.

### Phase 4 — Electron modal wiring

* In `main.js`: delete requests-panel view + IPCs; widen the modal overlay view bounds to `(0, 0, width, height)`; add `toggle-requests-modal` and `requests-modal-open` IPCs that load the new inbox URL.
* In `preload.js`: replace `toggleRequestsPanel` with `toggleRequestsModal`; add `openRequestsInbox(eventId)`.
* In `Chrome.jinja` / `chrome.js`: wire the requests-toggle to call `toggleRequestsModal` in Electron; handle the SSE `force_open_event_id` by calling `openRequestsInbox(eventId)`.
* The inbox modal page itself, when loaded in the overlay view, runs its own client-side JS for list+detail navigation (rerouting clicks, fetching fragments, posting grant/deny, handling auto-advance on resolution).
* Ship-ready state: full Electron experience works end-to-end. Browser mode still works via direct navigation to `/_chrome/requests-inbox`.

### Phase 5 — Browser-mode inline modal

* Add the hidden `#requests-inbox-modal` div in `Chrome.jinja`; in browser mode, the requests-toggle shows/hides it, fetches the list fragment, and wires up the detail-pane handlers.
* Browser mode subscribes to `"requests"` SSE for list refreshes and `force_open_event_id` triggers.
* Drop the cross-iframe `minds:open-request-modal` postMessage handler in `chrome.js` (no longer needed).
* Ship-ready state: browser and Electron parity.

### Phase 6 — Cleanup

* Delete `content-relay-preload.js`'s `minds:open-request-modal` relay (after confirming no remaining callers).
* Audit `app.py` for any orphaned helpers from the removed routes.
* Delete the old `static/sidebar.js`, `Sidebar.jinja`, and any orphaned imports.
* Add the changelog entry under `apps/minds/changelog/<branch-name>.md`.

## Testing strategy

### Unit tests

* `desktop_client/permission_routes_test.py` (and `latchkey/handlers/*_test.py`): adapt assertions that checked for full-page HTML output to also exercise the new `render_request_fragment` paths. Verify grant/deny still appends correct response events.
* New `desktop_client/requests_inbox_test.py`:
  * `GET /_chrome/requests-inbox` returns 200 with two-pane structure.
  * `GET /_chrome/requests-inbox/list` returns one card per pending request, sorted most-recent-first.
  * `GET /_chrome/requests-inbox/detail/<id>` returns the same body content as `render_request_fragment` for a known event.
  * Empty inbox returns the empty-state markup with the auto-open checkbox.
  * Detail endpoint with a resolved (`is_request_resolved`) id returns a "resolved" placeholder (similar to today's `_handle_request_page` post-resolution behavior).
* `desktop_client/templates_test.py` (or wherever home-page tests live): assert the rendered landing page includes the new open-in-new-window button per row with the expected SVG + onclick.
* SSE payload test: a pending `is_user_requested` request produces a `"requests"` event with `force_open_event_id` set to that event's id.

### Integration tests

* End-to-end Electron tests in `apps/minds/test_desktop_client_e2e.py`:
  * Open the app → home page renders → click the "open in new window" icon → new window opens with the right workspace.
  * Existing permission-dialog tests adapted to drive through the modal instead of the standalone `/requests/<id>` page.
  * Auto-open: with `auto_open_requests_panel=true`, simulate a new pending request and verify the modal opens with that event selected.
  * Manual open: click requests-toggle → modal opens with empty detail pane → click a list row → detail loads.
  * Resolve: grant a request → list shrinks, detail pane advances to next pending → grant the last → modal closes.
  * Empty state: open modal with no pending requests → empty-state visible, auto-open checkbox togglable.
  * `is_user_requested` request fires → modal auto-opens with that event selected.
* Browser-mode parity: same scenarios via `apps/minds/test_browser_e2e.py` (or whichever test harness covers browser-mode chrome).
* Sidebar absence: open any page; verify no `#sidebar-panel`, no `sidebar-toggle`, no `/_chrome/sidebar` route.

### Manual verification

* Activate a `dev-<user>` env, run `just minds-start`, exercise:
  * Sidebar gone, home page rows show open-in-new-window button, right-click context menu works.
  * Open in new window opens a second BaseWindow on the same workspace.
  * Trigger a permission request from a workspace; modal auto-opens covering the titlebar; clicking outside closes; reopen via chrome button; grant flow works; auto-advance to next request works.
  * Auto-open checkbox: disable, fire a new request, verify modal does NOT open; re-enable, verify modal does open.
  * `is_user_requested` request fires (e.g. via a latchkey browser-auth flow): inbox opens with that event selected even if auto-open is disabled.
* Edge cases:
  * Two windows open: each window's modal opens independently (existing modal-overlay-per-bundle behavior).
  * Trigger a request, then close the modal without resolving — the request stays pending, badge still shows, button reopens with it.
  * Resize the window while the modal is open — the 80%-width-with-cap responds; the modal stays centered.
  * Open the modal with `event_id=<unknown>` in the URL — falls back gracefully to empty detail pane.

### Ratchets and lint

* Run `just test-offload` for the full suite once the implementation lands.
* Run `/autofix` and `/verify-conversation` per the repo's stop-hook requirements.
* No new exceptions to the project's standard ratchets.

## Open questions

* **Modal width cap.** The plan suggests `max-w-[1280px]` for the two-pane layout. Worth confirming against the Figma sketch and existing modal sizes — the permission modal is `max-w-[640px]`, but a two-pane inbox at 640px feels cramped. Alternative: `max-w-screen-xl` (1280px) or `max-w-[1100px]`.
* **Detail pane on grant of the last request.** Plan says: auto-advance, modal closes when inbox empties. A "Resolved" confirmation flash (~1s) before closing might smooth the UX but adds a moving part. Default: close immediately, no flash.
* **`is_user_requested` priority when several user-requested events are pending.** If two user-requested events arrive in close succession while the modal is closed, `force_open_event_id` would only point at the most recent. The earlier one is still pending and visible in the list, just not auto-selected. Confirm this is OK (vs. opening multiple modals or queueing them).
* **Auto-open checkbox placement.** Today it's at the bottom of the right-side panel. In the new modal, it could go in: (a) the modal footer (spans full width), (b) below the list pane, (c) under a small kebab menu in the modal header. Default in plan: footer.
* **Workspace name conflicts.** The list pane shows `<kind>: <workspace-name>`. If two workspaces have the same name (e.g. across accounts), this is ambiguous. The sidebar grouped by account to disambiguate; the home page already shows them flat. Worth flagging if this becomes confusing in practice; could append a short account suffix.
* **Content-relay preload removal.** The cross-iframe `minds:open-request-modal` postMessage in `content-relay-preload.js` is no longer needed once `is_user_requested` covers all "summon the inbox" use cases. The plan keeps the IPC alive in Phase 4 as a redirect to the new URL, and deletes the preload in Phase 6 — but this depends on auditing whether any in-workspace content (e.g. system-interface views) still emits the postMessage.
* **Titlebar coverage during modal-open.** Plan widens the Electron modal-overlay view to `(0, 0, width, height)` so the modal panel can run top-to-bottom. Need to confirm the titlebar `WebContentsView` doesn't intercept clicks through the modal's transparent margins; if it does, we may need to call `setVisible(false)` on the titlebar view while the modal is open and restore it on close.
* **"Go to workspace" UX.** Plan adds an action in the detail pane that closes the modal and navigates the content view. If the user is already on that workspace, the navigate-to URL is a no-op but the modal still closes — confirm that's the desired behavior (alternatives: hide the button when current, or always show but make it a soft no-op).
