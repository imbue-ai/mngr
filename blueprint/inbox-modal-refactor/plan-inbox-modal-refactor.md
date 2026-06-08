# Plan: Inbox modal refactor

## Refined prompt

Currently we have two separate surfaces the requests panel that pushes the content over to the left when it's open and a modal surface where the permission dialogs are rendered. I want to move the requests to the modal surface as well (i.e. content won't be modified visually at all when the panel is open). Below is a sketch — for now let's not worry too much about the visual differences between what we have and this and instead concentrate mostly on the structural differences.

* Scope: permission requests only — drop "access requests" and "notifications" from the sketch (no section header, no scaffolding).
* Master/detail in one modal: left column lists pending permission requests; right pane shows the selected item's detail.
* Detail rendering: refactor handlers to expose a detail HTML fragment; the inbox page is server-rendered.
* On selection, swap only the right-pane fragment via fetch (no full-page reload on selection).
* Approve/Deny keeps the modal open and auto-advances to the next pending item.
* Empty state: when zero pending, the modal collapses to a single centered "No pending requests" message; reverts to master/detail when an item appears.
* Default selection: if opened via `navigate-to-request`, that item; otherwise the first pending item; otherwise empty state.
* Auto-open on a new pending request is gated by the existing `auto_open_requests_panel` setting; reopen-after-dismiss semantics match today (a brand-new id can reopen the inbox even after the user just closed it).
* SSE-driven list refresh: only the left list re-fetches and swaps; right pane is untouched unless the currently-selected item was the one that just resolved.
* If the currently-selected item is resolved by an SSE event (e.g. dismissed in another window, agent withdrew), the right pane swaps to a "This request is no longer available" message.
* Titlebar "requests" button opens the inbox modal; the `requestsPanelView` WebContentsView and its IPC channels are removed.
* The existing `navigate-to-request` IPC keeps working but now opens the inbox modal pre-selected on the target item.
* The standalone `/requests/<id>` route is removed; browser-only deep links use `/inbox?selected=<id>`.
* Stale `?selected=<id>`: the right pane shows "This request is no longer available"; the list still renders other pending items normally.
* Approve/Deny wiring: client makes two follow-up fetches after a successful grant/deny (next detail fragment + new list fragment) and swaps both panes.
* Handler shape: replace `render_request_page` with `render_request_detail_fragment(req_event, ...)` returning just the right-pane body.
* Per-handler scripts: delete the per-handler `extra_scripts` concept; inline the predefined-permission's "Adjust" toggle into the inbox shell JS directly.
* "Next pending" is computed by the client locally from the rendered list (next sibling in DOM order, skipping the just-resolved item).
* Tests: update existing release/acceptance tests as part of this work; add inbox-specific tests for auto-advance, stale `?selected`, empty state, and master/detail rendering.
* SSE debouncing: keep the existing 50 ms debounce in `main.js`; it now drives the inbox list-fragment refresh instead of a full panel reload.

## Overview

- The requests inbox today is a 320 px sidebar (`requestsPanelView`, a separate WebContentsView). Opening it shifts the workspace content view left, which is disruptive.
- Permission dialogs already render in a transparent, full-content-area modal overlay view (`modalView`). The plan is to make that same modal host the entire inbox: a master list on the left, the selected item's detail on the right.
- The user's workspace pixels never move when the inbox opens — same behavior as today's single-permission dialog.
- Visual fidelity to the sketch is explicitly de-prioritized for this pass; the goal is the structural refactor (one surface, master/detail, no content shift, no per-handler dialog chrome).
- The standalone `/requests/<id>` route disappears; deep links and `navigate-to-request` IPC both open the inbox modal pre-selected. Handlers stop returning full HTML pages and instead return right-pane fragments.

## Expected behavior

- The "requests" titlebar button opens the inbox modal in the current window. The modal overlays the workspace; the workspace is not resized.
- The inbox shows a master/detail layout when there is at least one pending request: left list (one card per pending item, grouped under a single "PERMISSION REQUESTS" header) and right pane (the selected item's detail form).
- When there are zero pending requests, the modal collapses to a single centered "No pending requests" message; the master/detail split is not rendered.
- Opening the inbox via `navigate-to-request` (from the titlebar bell click on a workspace notification, or from `minds:open-request-modal` postMessage) pre-selects the target item.
- Clicking a list item swaps the right pane's contents via fetch; the URL bar (browser mode) updates to `/inbox?selected=<id>` via `history.replaceState`.
- Approving an item posts to `/requests/<id>/grant`. On success, the client locally identifies the next pending item, fetches its detail fragment and the refreshed list fragment, and swaps both. The modal stays open.
- Denying an item posts to `/requests/<id>/deny` and follows the same flow.
- If the just-resolved item was the last one, the inbox transitions to the empty state in place. The modal does not auto-close.
- If an SSE `requests` event arrives while the inbox is open, the left list re-fetches and swaps. The right pane is untouched unless the currently-selected item was removed from the pending set; in that case the right pane swaps to a "This request is no longer available" message.
- Auto-open on a new pending request is governed by the existing `auto_open_requests_panel` setting. The reopen-after-dismiss diff (`hasNewRequest` in `main.js:1098`) still gates whether the modal is re-opened; the diff now drives `openModal(bundle, '/inbox')` instead of `openRequestsPanel(bundle)`.
- Backdrop click and Escape dismiss the inbox modal (same behavior as today's permission dialog).
- The titlebar request-count badge keeps working and reflects the pending count (no change to its data path).
- Browser-only mode: navigating to `/inbox?selected=<id>` shows the full inbox page directly (no Electron modal host); backdrop and Escape return to `/`.

## Implementation plan

### Server: routes and handlers

- `apps/minds/imbue/minds/desktop_client/app.py`
  - Add `_handle_inbox_page(request, auth_store)` → returns the server-rendered inbox shell at `GET /inbox`. Reads `?selected=<id>` query, validates against pending set, falls back to first-pending or empty.
  - Add `_handle_inbox_list_fragment(request, auth_store)` → returns just the left-list HTML at `GET /inbox/list`. Same auth + inbox-state lookup as the page route.
  - Add `_handle_inbox_detail_fragment(request_id, request, auth_store, backend_resolver)` → returns just the right-pane HTML at `GET /inbox/detail/{request_id}`. On unknown / already-resolved id, returns the "no longer available" fragment with HTTP 200 (so the client can swap it directly).
  - Wire all three routes into `_register_routes` near the existing request handlers.
  - Delete `_handle_requests_panel` and the `GET /_chrome/requests-panel` route registration.
  - Delete the `GET /requests/{request_id}` route (`_handle_request_page` and its registration). Keep the `POST /requests/{id}/grant` and `POST /requests/{id}/deny` routes unchanged.

- `apps/minds/imbue/minds/desktop_client/request_handler.py`
  - Replace the abstract `render_request_page(req_event, backend_resolver, mngr_forward_origin) -> Response` with `render_request_detail_fragment(req_event, backend_resolver, mngr_forward_origin) -> str`. The return is now an HTML fragment (no `<html>`, no backdrop, no close button, no per-handler `<script>` blocks).
  - Update the docstring to reflect the fragment contract.

- `apps/minds/imbue/minds/desktop_client/latchkey/handlers/predefined.py`
  - Rename `render_request_page` → `render_request_detail_fragment`.
  - Return the body produced by `render_predefined_permission_dialog`, stripped of the `PermissionsDialog` chrome (backdrop, close button, generic `<script>`).
  - Drop the `extra_scripts` arg path (no per-handler JS).

- `apps/minds/imbue/minds/desktop_client/latchkey/handlers/file_sharing.py`
  - Same treatment: rename and switch to fragment-only return.

- `apps/minds/imbue/minds/desktop_client/latchkey/handlers/templates.py`
  - Update `render_predefined_permission_dialog` / `render_file_sharing_permission_dialog` to render new fragment-only JinjaX components (or update the existing `pages.Latchkey*Permission` components to render their bodies without a `<PermissionsDialog>` wrapper).
  - Move the `showPermissionEditor()` "Adjust" toggle out of the per-handler component; it becomes inbox-shell JS.

### Server: templates

- `apps/minds/imbue/minds/desktop_client/templates/pages/Inbox.jinja` (new)
  - Backdrop + dialog card with master/detail layout. Renders the empty-state branch when `pending_count == 0`.
  - Left list: one card per pending item with `data-request-id`, `data-agent-id`, click handler hooked by inbox JS.
  - Right pane: contains the server-rendered initial detail fragment (or the empty-state placeholder, or the "no longer available" placeholder for an invalid `?selected`).
  - Includes inbox shell `<script>`: selection swap, "Adjust" toggle, Approve/Deny submit + auto-advance, escape/backdrop dismiss, empty-state transitions.

- `apps/minds/imbue/minds/desktop_client/templates/pages/InboxList.jinja` (new)
  - The same left-list markup, factored out so `/inbox/list` can render the fragment without rebuilding the whole page.

- `apps/minds/imbue/minds/desktop_client/templates/pages/InboxEmpty.jinja` (new)
  - The centered "No pending requests" placeholder.

- `apps/minds/imbue/minds/desktop_client/templates/pages/InboxUnavailable.jinja` (new)
  - The right-pane "This request is no longer available" fragment, served for stale `?selected` / SSE-resolved-while-selected.

- `apps/minds/imbue/minds/desktop_client/templates/pages/LatchkeyPredefinedPermission.jinja`
  - Strip `PermissionsDialog` chrome. Remove the inline `<script>` (`showPermissionEditor` moves to inbox shell JS). The body becomes the fragment that the inbox right pane swaps into.

- `apps/minds/imbue/minds/desktop_client/templates/pages/LatchkeyFileSharingPermission.jinja`
  - Same: strip the chrome; the body becomes the fragment.

- `apps/minds/imbue/minds/desktop_client/templates/PermissionsDialog.jinja`
  - Delete. The generic script, backdrop, and close button all move into the inbox shell.

- `apps/minds/imbue/minds/desktop_client/templates/PermissionsForm.jinja` (if it exists)
  - Keep the form structure (Approve / Deny buttons, hidden inputs) — it's the per-handler-rendered form that the inbox-shell JS submits. Remove anything that assumes a full-page dialog host.

### Electron main process

- `apps/minds/electron/main.js`
  - Remove `REQUESTS_PANEL_WIDTH` constant and its use in `updateBundleBounds` (`rightOffset` line).
  - Remove `bundle.requestsPanelView`, `bundle.requestsPanelVisible`, `bundle.requestsPanelReloadTimer`, and their cleanup in the bundle `close` handler.
  - Delete `openRequestsPanel`, `closeRequestsPanel`, `toggleRequestsPanel`, `scheduleRequestsPanelReload`, `REQUESTS_PANEL_RELOAD_DEBOUNCE_MS`.
  - In the `requests` SSE handler (`main.js:1086`): keep the `idsChanged` / `hasNewRequest` diff and the existing 50 ms debounce. On `shouldAutoOpen`, call `openModal(bundle, backendBaseUrl + '/inbox')` for bundles whose modal isn't already showing the inbox. On `idsChanged`, send a `chrome-event` (or new `inbox-event`) to any open inbox modal view to trigger a list-fragment refetch; reuse the same debounce timer wiring per bundle, just retargeted at the modal view instead of the panel view.
  - In `openModal`: accept a query/fragment so the same modal view can serve `/inbox` vs `/inbox?selected=<id>` without churn. The transparent-overlay layout (`main.js:262`) is unchanged.
  - In `ipcMain.on('navigate-to-request')` (`main.js:1935`): switch the URL from `/requests/<eventId>` to `/inbox?selected=<eventId>`.
  - In `ipcMain.on('open-request-modal')` (`main.js:1952`): same switch.
  - Remove the `toggle-requests-panel` and `open-requests-panel` IPC handlers.
  - Add `toggle-inbox` (or repurpose `toggle-requests-panel`) IPC: opens the modal at `/inbox`, or closes it if already open and showing the inbox.

- `apps/minds/electron/preload.js`
  - Remove `toggleRequestsPanel` and `openRequestsPanel` from the `window.minds` bridge.
  - Add `toggleInbox` (or keep the same name for back-compat with the titlebar JS that already calls `window.minds.toggleRequestsPanel()`).

### Chrome JS (titlebar)

- `apps/minds/imbue/minds/desktop_client/static/chrome.js`
  - Update `document.getElementById('requests-toggle').onclick` to call the new `toggleInbox` IPC.
  - `updateRequestsBadge` keeps working unchanged (driven by `requests` SSE).

- `apps/minds/imbue/minds/desktop_client/templates/pages/Chrome.jinja`
  - `requests-toggle` button keeps its id, title, and badge; only the click target changes.

### Browser-mode fallback

- `apps/minds/imbue/minds/desktop_client/static/chrome.js`
  - In the `window.addEventListener('message', ...)` handler (`chrome.js:215`), switch the navigation target from `/requests/<id>` to `/inbox?selected=<id>` so non-Electron browsing also routes through the inbox page.

### Config and naming

- `apps/minds/imbue/minds/desktop_client/minds_config.py`
  - Keep the `auto_open_requests_panel` key as-is to avoid a config migration. Add a docstring note that "panel" now means the inbox modal. (Renaming is out of scope; revisit in a follow-up.)
  - `POST /_chrome/requests-auto-open` route stays; only its consumers change.

### Inbox shell JS (lives in `Inbox.jinja` initially)

- Selection click handler:
  - Reads `data-request-id` from the clicked card.
  - Calls `fetch('/inbox/detail/' + id)` and replaces `#inbox-detail` innerHTML with the response.
  - Updates `history.replaceState` to `/inbox?selected=<id>`.
  - Updates an `is-selected` class on the card and removes it from siblings.
- "Adjust" toggle handler:
  - Delegated to the inbox shell; looks for `#permissions-simple-view` / `#permissions-editor-view` within the right pane and toggles their `hidden` classes.
- Approve submit handler:
  - Delegated to the inbox shell; intercepts form submits inside `#inbox-detail`.
  - Reads form action (handler's POST URL); POSTs the form data; on `GRANTED`, computes the next pending id locally from the list DOM (next sibling card, skipping the resolved one), fetches `/inbox/detail/<next>` and `/inbox/list`, swaps both. Handles `NEEDS_MANUAL_CREDENTIALS` and error responses inline in the right pane.
- Deny handler:
  - Fire-and-forget POST to the `/deny` sibling URL; then runs the same "fetch next detail + new list" flow.
- Empty-state transition:
  - When `/inbox/list` returns "no items", the shell removes the master/detail split (or replaces the dialog body with the empty-state placeholder).
- Escape + backdrop:
  - Escape closes the modal: `window.minds.closeModal()` in Electron, `window.location.href = "/"` in browser mode (mirroring today's `closePermissionDialog`).
  - Backdrop click closes the modal (same).
- SSE listener (browser mode only):
  - If running standalone (non-Electron), subscribes to the existing `_chrome/sse` requests stream and triggers list re-fetches. In Electron, the main process drives this via the IPC `chrome-event` push.

## Implementation phases

Single atomic change — there is no intermediate state where the panel and the inbox modal coexist. The list below is the suggested editing order within that one change, not a sequence of separately landable steps. The branch is not green until every section is done.

### 1. Strip per-handler dialog chrome

- Edit `templates/pages/LatchkeyPredefinedPermission.jinja` to remove its `<PermissionsDialog>` wrapper and its inline `showPermissionEditor` `<script>`. The file now renders a bare fragment (header + form + manual creds + error placeholder).
- Edit `templates/pages/LatchkeyFileSharingPermission.jinja` the same way.
- Delete `templates/PermissionsDialog.jinja` (chrome lives in the inbox shell from now on).

### 2. Add inbox templates

- Add `templates/pages/Inbox.jinja`: backdrop, dialog card, master/detail layout, empty-state branch, and the inbox shell `<script>`.
- Add `templates/pages/InboxList.jinja`: the left-list fragment.
- Add `templates/pages/InboxEmpty.jinja`: the centered "No pending requests" placeholder.
- Add `templates/pages/InboxUnavailable.jinja`: the right-pane "no longer available" fragment.

### 3. Refactor handler contract and add inbox routes

- In `request_handler.py`, replace `render_request_page` with `render_request_detail_fragment(req_event, backend_resolver, mngr_forward_origin) -> str`.
- Update `latchkey/handlers/predefined.py`, `latchkey/handlers/file_sharing.py`, and the test stub in `permission_routes_test.py` (`class _StubHandler`) to implement the new method and stop returning page-shape responses.
- Update `latchkey/handlers/templates.py` so the latchkey render functions return fragments (and drop the `extra_scripts` argument plumbing).
- In `app.py`, add `_handle_inbox_page`, `_handle_inbox_list_fragment`, `_handle_inbox_detail_fragment` and register them at `GET /inbox`, `GET /inbox/list`, `GET /inbox/detail/{id}`.
- In `app.py`, delete `_handle_requests_panel`, `_handle_request_page`, and the `GET /_chrome/requests-panel` and `GET /requests/{id}` route registrations. Keep the `POST /requests/{id}/grant` and `/deny` routes unchanged.

### 4. Electron main process: swap the surface

- In `electron/main.js`:
  - Delete `REQUESTS_PANEL_WIDTH`, `REQUESTS_PANEL_RELOAD_DEBOUNCE_MS`, `openRequestsPanel`, `closeRequestsPanel`, `toggleRequestsPanel`, `scheduleRequestsPanelReload`. Reintroduce the 50 ms constant under a new name (e.g. `INBOX_LIST_REFRESH_DEBOUNCE_MS`).
  - Remove `bundle.requestsPanelView`, `bundle.requestsPanelVisible`, `bundle.requestsPanelReloadTimer` from bundle creation, the `close` cleanup, and `updateBundleBounds`. Drop the `rightOffset` calculation so the content view always uses the full width.
  - Replace the `ipcMain.on('toggle-requests-panel', ...)` and `ipcMain.on('open-requests-panel', ...)` handlers with a single `ipcMain.on('toggle-inbox', ...)` that opens the modal at `/inbox` (or closes it if already showing the inbox).
  - In `ipcMain.on('navigate-to-request', ...)` (`main.js:1935`) and `ipcMain.on('open-request-modal', ...)` (`main.js:1952`), change the URL to `/inbox?selected=<eventId>`.
  - In the SSE `requests` handler (`main.js:1086`): keep the existing `idsChanged` / `hasNewRequest` diff. On `idsChanged`, post a `chrome-event` to any open inbox modal view (debounced 50 ms per bundle) so its shell JS re-fetches the list fragment. On `shouldAutoOpen`, call `openModal(bundle, backendBaseUrl + '/inbox')`.
- In `electron/preload.js`: remove `toggleRequestsPanel` and `openRequestsPanel`; add `toggleInbox` (wired to the new IPC channel).

### 5. Titlebar and browser-only fallback

- In `static/chrome.js`:
  - Point the `requests-toggle` click at `window.minds.toggleInbox()`.
  - In the browser-mode `window.addEventListener('message', ...)` handler (`chrome.js:215`), navigate the content frame to `/inbox?selected=<id>` instead of `/requests/<id>`.

### 6. Tests, changelog, and config note

- Update `test_desktop_client.py` and `permission_routes_test.py` per the Testing strategy section.
- Update `latchkey/handlers/predefined_test.py` and `latchkey/handlers/file_sharing_test.py` to assert against the fragment shape.
- Add `apps/minds/changelog/<branch>.md` describing the user-visible change.
- Add a docstring note to the `auto_open_requests_panel` config key in `minds_config.py` clarifying that "panel" now means the inbox modal (no rename in this change).

## Testing strategy

### Unit tests (Python; pytest)

- `test_desktop_client.py`:
  - `test_inbox_requires_auth` — `/inbox` without a session returns the "Not authenticated" body.
  - `test_inbox_empty_state` — with no pending requests, the body contains the empty-state placeholder and not the master/detail split.
  - `test_inbox_master_detail_renders_first_pending` — with one pending event, the body contains the list card AND the rendered detail form.
  - `test_inbox_preselects_query_param` — `?selected=<id>` of a pending event server-renders that detail, not the first one.
  - `test_inbox_stale_selected_renders_unavailable` — `?selected=<unknown_id>` server-renders the "no longer available" right-pane placeholder; the list still renders other pending items.
  - `test_inbox_resolved_selected_renders_unavailable` — `?selected=<resolved_id>` does the same.
  - `test_inbox_list_fragment_route` — `GET /inbox/list` returns just the list HTML (no `<html>`).
  - `test_inbox_detail_fragment_route` — `GET /inbox/detail/<id>` returns just the detail HTML.
  - `test_inbox_detail_fragment_for_unknown_id_returns_unavailable_200` — semantics for the SSE-resolved-while-selected case.
  - `test_old_requests_page_route_removed` — `GET /requests/<id>` returns 404 (route is gone).
  - `test_auto_open_toggle` — unchanged (the config key persists).
  - Delete `test_requests_panel_requires_auth`, `test_requests_panel_shows_empty_inbox`, `test_requests_panel_card_routes_via_minds_bridge`, `test_request_page_not_found`.

- `permission_routes_test.py`:
  - Update every `client.get(f"/requests/{request.event_id}")` to `client.get(f"/inbox/detail/{request.event_id}")` and assert against the fragment shape (no chrome).
  - Keep `POST /requests/<id>/grant` and `POST /requests/<id>/deny` tests unchanged.
  - Add: `test_grant_response_shape_supports_client_auto_advance` — the JSON response from POST grant still has the `outcome` and `message` fields the inbox shell JS reads.

- `latchkey/handlers/predefined_test.py` and `latchkey/handlers/file_sharing_test.py`:
  - Replace any tests on the page-shape handler output with assertions on the fragment shape (no `<html>` tag, no backdrop id, has the Approve / Deny buttons).

- New `inbox_routes_test.py` (or fold into `test_desktop_client.py`):
  - `test_inbox_detail_fragment_does_not_include_dialog_chrome` — fragment has no `#permissions-backdrop`, no `#permissions-close-btn`, no top-level `<html>`.
  - `test_inbox_list_orders_by_timestamp_descending` — same ordering as the old panel.

### Manual / integration tests

- Open inbox from titlebar button: workspace pixels don't shift; modal overlays correctly.
- Click a list item: right pane swaps without a full reload; URL bar updates.
- Approve: modal stays open; auto-advances to next pending; list updates.
- Deny: same.
- Resolve the last pending item: modal collapses to empty state in place; does not auto-close.
- Open inbox with zero pending: empty state renders.
- New pending request arrives while inbox is open: list re-fetches; right pane is untouched.
- Currently-selected item resolved externally (denied in another window): right pane shows "no longer available" message.
- Auto-open: with `auto_open_requests_panel = True`, a new request opens the inbox modal. With it `False`, only the badge updates.
- Re-open after dismiss: close the inbox; a brand-new id arriving reopens it (matching today's `hasNewRequest` semantics).
- `navigate-to-request` from a workspace notification: opens the inbox modal pre-selected on the target item.
- Browser-only mode: `/inbox?selected=<id>` renders as a normal page; escape closes via `window.location.href = '/'`.

### Edge cases

- Pre-selected id is valid at server-render time but resolved between render and the user's first interaction: the right pane still shows the original fragment, but submitting Approve will hit the resolved-request path on the server and return an error, which the inbox shell JS surfaces inline.
- Inbox modal already open when SSE fires: `idsChanged` triggers a list refresh; `shouldAutoOpen` is a no-op because the modal is already showing.
- Backdrop click while a POST grant is in flight: today the dialog dismisses; here we should let the in-flight POST complete (`keepalive: true`) before the page unloads. Match today's behavior.
- Rapid Approve clicks: the shell JS disables the Approve button on submit (same as today's `approveBtn.disabled = true`).

## Open questions

- Should the `auto_open_requests_panel` config key be renamed to `auto_open_inbox` at the same time, or leave the rename for a follow-up? (Plan currently keeps it as-is.)
- How should `kind_label()` and `display_name_for_event()` evolve once the inbox owns rendering? They currently feed the panel card label; the inbox list will also need them. Confirm we keep both methods on `RequestEventHandler` with their current contracts.
- Is the master/detail layout owned by the inbox `<Base>` page, or should we introduce a lightweight `InboxLayout.jinja` JinjaX component for reuse if other modal-hosted master/detail surfaces appear later?
- When a new pending request arrives while the inbox is open and showing the empty state, the shell needs to switch from empty-state to master/detail mid-life. Do we do this by hot-swapping the modal body, or by reloading `/inbox` in the modal view? (Hot-swap is more in keeping with the rest of the design.)
- Should the inbox list also re-fetch on focus (e.g. when the user switches back to the window after a long pause)? Today the panel reloads on every open; the new modal model could keep the same idea by triggering a list re-fetch on `visibilitychange`.
- Should `/inbox/detail/<id>` for a resolved or unknown id return HTTP 200 with the unavailable fragment (so the JS can innerHTML-swap directly), or 404 with a body the JS has to special-case? Plan currently assumes 200 + fragment.
- Does the existing `PermissionsManualCredentials` / `PermissionsError` flow need any per-handler tweaks when its host moves from a per-permission dialog to the inbox right pane, or do those JinjaX components compose cleanly inside the fragment?
- What's the right keyboard shortcut for "open inbox" (today there's none; the titlebar button is mouse-only)? Out of scope here, but worth a follow-up.
