# Workspace-Account Association and Request Inbox

## Overview

* The minds app currently has a single global SuperTokens session (`~/.minds/supertokens/supertokens_session.json`). Sharing (Cloudflare tunnels) uses that single account. This creates two problems: (1) there is no way to associate different workspaces with different accounts, and (2) sharing/permissions logic lives inside the `minds_workspace_server` (the wrong abstraction layer).
* This spec introduces two tightly-related changes:
  1. **Multi-account sessions with per-workspace association** -- workspaces are associated with at most one account ("private" if none). Accounts are managed from a new "Manage account(s)" page. A `sessions.json` file replaces the old single-session store.
  2. **Request inbox** -- agents write request events (sharing, permissions) to `requests/events.jsonl`. The desktop client watches these via `mngr event --follow`, shows them in a right-side inbox panel, and handles the actual sharing/permissions UI. The workspace server's share dialog becomes read-only; edits trigger request events.
* The two pieces are done together because sharing is intimately tied to which account owns the tunnel -- the account comes from the workspace association, not a global session.
* No backward compatibility is needed (nothing is in production yet).

## Expected Behavior

### Multi-account and workspace association

* Users can be logged into multiple accounts simultaneously. Sessions are stored in `~/.minds/sessions.json` as a map of user IDs to session data (including `workspace_ids` list per account).
* The old `~/.minds/supertokens/` directory and `SuperTokensSessionStore` single-session logic are removed entirely.
* The "Manage account(s)" link in the title bar replaces the current email/sign-out display. If no accounts are logged in, "Log in" is shown instead.
* The "Manage account(s)" page lists all logged-in accounts. From it, you can:
  - Click an account to see its details/settings page
  - Click "Add account" to initiate the OAuth flow (which adds a new entry or updates existing tokens if the user ID already exists)
  - Click "Log out" next to any account (removes tokens but does NOT auto-disassociate workspaces; they become non-functional for sharing until re-login)
  - Set one account as the "default" for new workspaces (stored in `~/.minds/config.toml`)
* The left sidebar groups workspaces by account, with "Private" workspaces shown at the top, then accounts alphabetically.
* The main workspace table shows the associated account for each workspace.
* The workspace table's "setup" link leads to a settings page where the user can:
  - Disassociate the workspace from its current account (with a warning that all tunnels will be torn down)
  - Associate a "Private" workspace with a logged-in account
* When disassociating, the entire Cloudflare tunnel is deleted (not just services). The workspace gets a clean slate when re-associated.
* The workspace creation form includes an "Account" dropdown with a "Private (no account)" option and all logged-in accounts. The default account (from `config.toml`) is pre-selected if set.

### Request inbox

* The share dialog in `minds_workspace_server` becomes **read-only**: it shows whether sharing is enabled, the shared URL (with copy button), and who has access. No edits can be made from within this dialog.
* Clicking "Edit sharing" in the read-only dialog:
  - Writes a request event to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` (including current state as structured data for pre-populating the desktop client form, and an `is_user_requested: true` flag)
  - Closes the dialog with a brief toast ("Sharing request sent")
* The sharing proxy (`sharing_proxy.py`) retains its GET endpoint (read-only status queries still proxy to the desktop client via reverse SSH tunnel). PUT/DELETE mutation endpoints are removed and replaced with local request event writes.
* The desktop client watches `requests/events.jsonl` for all workspace agents by adding `requests` as a second source alongside `servers` in the existing `_start_events_stream` call (`mngr event <agent-id> servers requests --follow --quiet`).
* `requests/events.jsonl` is pre-created (via `touch`) alongside `servers/events.jsonl` in `host.py` during agent state directory setup, in a single touch command for lower latency.
* When a new request event arrives:
  - A desktop notification is shown. Clicking it navigates the content view to `/requests/<request_id>`.
  - If `is_user_requested` is true, the content view auto-navigates to the request editing page immediately.
  - The right-side inbox panel opens automatically if the "auto-open" setting is enabled (default: true).
* The right-side inbox panel:
  - Implemented as a new Electron `WebContentsView` (like the left sidebar), persisting across content page navigations.
  - Hidden by default; has a toggle button with notification badge in the chrome title bar (right side, near window controls).
  - Shows all pending requests as cards, most recent first.
  - Has a checkbox at the bottom: "Automatically open upon new request" (checked by default), persisted in `~/.minds/config.toml`.
* Requests are deduplicated by `(agent_id, server_name, request_type)` -- only the most recent request for the same target appears in the inbox.
* The sharing request editing page in the desktop client (at `/requests/<request_id>`):
  - Shows the same sharing UI that previously lived in the workspace server (email list, enable/disable toggle, auth policies).
  - Pre-populates from the structured data in the request event.
  - Shows the shared URL with copy button (the whole point of enabling sharing is to get the URL).
  - Uses the account associated with the workspace to determine tunnel ownership.
* When a request is granted or denied, a response event is appended to `~/.minds/events/requests/events.jsonl` with `status: "granted"` or `"denied"`, the original request's `event_id`, and the dedup key. The request is then no longer shown in the inbox.
* Permissions request type: events are created the same way, notifications fire the same way, but the editing UI just displays the request content for now (no interactive form).

### Settings file

* `~/.minds/config.toml` is a new minds-specific settings file storing:
  - `default_account_id` (user ID of the default account for new workspaces)
  - `auto_open_requests_panel` (boolean, default true)

## Implementation Plan

### Data types and storage

* **`apps/minds/imbue/minds/desktop_client/session_store.py`** (new) -- `MultiAccountSessionStore` class replacing `SuperTokensSessionStore`:
  - Stores/loads `~/.minds/sessions.json` (a dict mapping `SuperTokensUserId` -> `AccountSession`)
  - `AccountSession` (new FrozenModel): `access_token`, `refresh_token`, `user_id`, `email`, `display_name`, `workspace_ids: list[str]`
  - Thread-safe via lock on all reads/writes
  - Methods: `add_or_update_session()`, `remove_session(user_id)`, `load_all_sessions()`, `get_session(user_id)`, `get_access_token(user_id)` (with auto-refresh), `associate_workspace(user_id, agent_id)`, `disassociate_workspace(user_id, agent_id)`, `get_account_for_workspace(agent_id)`, `list_accounts()`, `get_user_info(user_id)`
  - File: `~/.minds/sessions.json`, permissions `0o600`

* **`apps/minds/imbue/minds/desktop_client/minds_config.py`** (new) -- `MindsConfig` class:
  - Reads/writes `~/.minds/config.toml`
  - Fields: `default_account_id: str | None`, `auto_open_requests_panel: bool` (default True)
  - Thread-safe via lock

* **`apps/minds/imbue/minds/desktop_client/request_events.py`** (new) -- Request/response event types:
  - `RequestEvent(EventEnvelope)`: base for all request events, fields: `agent_id: str`, `request_type: str` (e.g., "sharing", "permissions"), `is_user_requested: bool`
  - `SharingRequestEvent(RequestEvent)`: adds `server_name: str`, `current_status: SharingStatus | None` (pre-populated state), `suggested_emails: list[str]`
  - `PermissionsRequestEvent(RequestEvent)`: adds `resource: str`, `description: str`
  - `RequestResponseEvent(EventEnvelope)`: fields: `request_event_id: str`, `status: str` ("granted" / "denied"), `agent_id: str`, `server_name: str | None`, `request_type: str`
  - `RequestInbox` class: aggregates request + response events to compute pending requests; `add_request()`, `add_response()`, `get_pending_requests()`, `get_request_by_id()`

* **`apps/minds/imbue/minds/desktop_client/supertokens_auth.py`** -- Remove `SuperTokensSessionStore` class and all single-session logic. Keep `UserInfo`, `derive_user_id_prefix`, `SuperTokensAccessToken`, `SuperTokensRefreshToken`, `SuperTokensUserId` type definitions. The token refresh logic moves into `MultiAccountSessionStore`.

* **`libs/imbue_common/imbue/imbue_common/event_envelope.py`** -- No changes (already has `EventEnvelope`).

* **`apps/minds/imbue/minds/desktop_client/backend_resolver.py`** -- Convert `ServerLogRecord` to inherit from `EventEnvelope`. Update `parse_server_log_record()` / `parse_server_log_records()` accordingly.

### Agent state setup (mngr core)

* **`libs/mngr/imbue/mngr/hosts/host.py`** (lines ~2139-2146) -- Add `requests_events_dir = events_dir / "requests"` to the `_mkdirs` list. Combine the `touch` into a single command: `touch '{servers_events_file}' '{requests_events_file}'`.

### Workspace server changes

* **`apps/minds_workspace_server/frontend/src/views/ShareModal.ts`** -- Convert to read-only:
  - Remove `enableSharing()`, `disableSharing()`, `updateAuth()`, `addEmail()`, `removeEmail()` functions
  - Keep `fetchStatus()` for reading current state
  - Replace the enable/disable/update buttons with an "Edit sharing" button that POSTs to a new `/api/sharing/<serverName>/request` endpoint
  - Keep the URL display with copy button
  - Show email list as read-only (no add/remove)

* **`apps/minds_workspace_server/imbue/minds_workspace_server/server.py`** -- Add `/api/sharing/<serverName>/request` POST endpoint that:
  - Reads current sharing status via the existing GET proxy
  - Writes a `SharingRequestEvent` to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl`
  - Returns success; the frontend shows a toast and closes the dialog

* **`apps/minds_workspace_server/imbue/minds_workspace_server/sharing_proxy.py`** -- Remove `enable_sharing()`, `update_sharing_auth()`, `disable_sharing()` functions and the `_cloudflare_url` helper for mutations. Keep `get_sharing_status()` and the read infrastructure (`_read_minds_api_url`, `_get_desktop_client_auth_headers`, `SharingStatus`).

### Desktop client backend changes

* **`apps/minds/imbue/minds/desktop_client/app.py`** -- Major changes:
  - Replace `SuperTokensSessionStore` dependency with `MultiAccountSessionStore`
  - Add `MindsConfig` dependency
  - Add `RequestInbox` dependency (loaded from `~/.minds/events/requests/events.jsonl` on startup)
  - New routes:
    - `GET /accounts` -- manage accounts page
    - `POST /accounts/set-default` -- set default account
    - `POST /accounts/<user_id>/logout` -- log out an account
    - `GET /workspace/<agent_id>/settings` -- workspace settings page (associate/disassociate)
    - `POST /workspace/<agent_id>/associate` -- associate workspace with account
    - `POST /workspace/<agent_id>/disassociate` -- disassociate (with tunnel teardown)
    - `GET /requests/<request_id>` -- request editing page (sharing dialog)
    - `POST /requests/<request_id>/grant` -- grant a request (writes response event, executes sharing action)
    - `POST /requests/<request_id>/deny` -- deny a request (writes response event)
    - `GET /_chrome/requests-panel` -- HTML for the right-side inbox panel
    - `GET /_chrome/requests-events` -- SSE endpoint streaming request inbox updates
  - Update `_handle_chrome_events()` to also stream request counts (for badge)
  - Update `_handle_landing_page()` to show account column in workspace table
  - Update create form to include account dropdown
  - Remove the old user email display from chrome template; replace with "Log in" / "Manage account(s)" link

* **`apps/minds/imbue/minds/desktop_client/runner.py`** -- Update startup:
  - Initialize `MultiAccountSessionStore` instead of `SuperTokensSessionStore`
  - Initialize `MindsConfig`
  - Initialize `RequestInbox` (replay `~/.minds/events/requests/events.jsonl` on startup)
  - SuperTokens init remains the same (OAuth callback updates `MultiAccountSessionStore`)

* **`apps/minds/imbue/minds/desktop_client/backend_resolver.py`** -- Changes:
  - `_start_events_stream()` passes both `servers` and `requests` sources: `[self.mngr_binary, "events", aid_str, SERVERS_EVENT_SOURCE_NAME, "requests", "--follow", "--quiet"]`
  - `_on_events_stream_output()` differentiates between server events and request events by checking the `source` field (from `EventEnvelope`)
  - New callback: `add_on_request_callback()` to notify the app when request events arrive
  - Convert `ServerLogRecord` to inherit from `EventEnvelope`

* **`apps/minds/imbue/minds/desktop_client/cloudflare_client.py`** -- Add `delete_tunnel(agent_id)` method for tunnel teardown during disassociation. Update `make_tunnel_name()` and `effective_owner_email()` to accept a user ID parameter (from the workspace's associated account) instead of using the global session.

* **`apps/minds/imbue/minds/desktop_client/api_v1.py`** -- Update `get_cf_client_with_auth()` to look up the account associated with the workspace (via `MultiAccountSessionStore`) instead of using a global session. Update tunnel token injection to use per-workspace account.

* **`apps/minds/imbue/minds/desktop_client/notification.py`** -- Add `url` field to the notification JSONL payload so Electron can navigate on click.

### Electron changes

* **`apps/minds/electron/main.js`** -- Changes:
  - Add `requestsPanelView` as a new `WebContentsView` (right-side panel, similar to `sidebarView`)
  - Add `toggleRequestsPanel()` function and `ipcMain.on('toggle-requests-panel', ...)` handler
  - Update `updateViewBounds()` to account for right panel width when visible
  - Handle notification click events: use the `url` field from notification JSONL to navigate `contentView`
  - Add `ipcMain.on('open-requests-panel', ...)` for programmatic opening (from SSE events)

* **`apps/minds/electron/backend.js`** -- Update notification handler to:
  - Add click handler to `Notification` that sends IPC to navigate content view
  - Parse `url` field from notification JSONL event

* **`apps/minds/electron/preload.js`** -- Add `toggleRequestsPanel()` and `openRequestsPanel()` to the `window.minds` IPC bridge.

### Templates

* **`apps/minds/imbue/minds/desktop_client/templates.py`** -- Changes:
  - Update `_CHROME_TEMPLATE`: replace user email/sign-out with "Log in" / "Manage account(s)" link; add requests panel toggle button with badge on the right side of the title bar
  - Update `_LANDING_PAGE_TEMPLATE`: add "Account" column to workspace table
  - Update `_CREATE_FORM_TEMPLATE`: add "Account" dropdown
  - Add `_ACCOUNTS_PAGE_TEMPLATE`: manage accounts page (list accounts, add/remove, set default)
  - Add `_WORKSPACE_SETTINGS_TEMPLATE`: workspace settings page (associate/disassociate)
  - Add `_REQUESTS_PANEL_TEMPLATE`: right-side inbox panel (request cards, auto-open toggle)
  - Add `_SHARING_REQUEST_PAGE_TEMPLATE`: sharing request editing page (email list, enable/disable, URL display)
  - Update `_SIDEBAR_TEMPLATE`: group workspaces by account with headers

### File cleanup

* Delete `apps/minds/imbue/minds/desktop_client/supertokens_auth.py` (replaced by `session_store.py`; type definitions like `SuperTokensAccessToken` move to `session_store.py` or a shared types module)
* Remove `~/.minds/supertokens/` directory handling from all code

## Implementation Phases

### Phase 1: Multi-account session store and config

Build the new storage layer without changing any UI.

* Create `session_store.py` with `MultiAccountSessionStore`
* Create `minds_config.py` with `MindsConfig`
* Update `runner.py` to initialize both (keep old auth flow working temporarily by delegating to the new store)
* Update the SuperTokens OAuth callback to write to `sessions.json` instead of `supertokens_session.json`
* Delete `supertokens_auth.py` and migrate type definitions
* Tests: unit tests for `MultiAccountSessionStore` (add/remove sessions, workspace association, token refresh, locking) and `MindsConfig` (read/write TOML, defaults)

### Phase 2: Workspace-account association UI

Wire up the multi-account store to the UI.

* Update chrome template: "Log in" / "Manage account(s)" link
* Create the "Manage account(s)" page (list, add, logout, set default)
* Update workspace table to show account column
* Update workspace creation form with account dropdown
* Create workspace settings page (associate/disassociate)
* Update left sidebar to group workspaces by account
* Implement tunnel teardown on disassociation (`delete_tunnel` in `cloudflare_client.py`)
* Update `api_v1.py` and `cloudflare_client.py` to use per-workspace account for tunnel operations
* Tests: integration tests for account management routes, workspace association/disassociation, tunnel teardown

### Phase 3: Request event infrastructure

Build the event plumbing without UI.

* Create `request_events.py` with event types and `RequestInbox`
* Update `host.py` to pre-create `requests/events.jsonl` (single `touch` command)
* Convert `ServerLogRecord` to inherit from `EventEnvelope`
* Update `_start_events_stream` to subscribe to both `servers` and `requests` sources
* Update `_on_events_stream_output` to differentiate and route request events
* Add request callback mechanism to `MngrCliBackendResolver`
* Initialize `RequestInbox` on startup with event replay from `~/.minds/events/requests/events.jsonl`
* Tests: unit tests for `RequestInbox` (add request, add response, dedup, pending computation), event parsing, `ServerLogRecord` with `EventEnvelope`

### Phase 4: Workspace server share dialog changes

Convert the share dialog to read-only + request creation.

* Update `ShareModal.ts` to be read-only (remove mutation functions, add "Edit sharing" button)
* Add `/api/sharing/<serverName>/request` POST endpoint in `server.py`
* Remove mutation functions from `sharing_proxy.py`
* Add toast notification on request creation
* Tests: unit tests for the new request endpoint, integration test for the read-only dialog flow

### Phase 5: Desktop client request UI and notifications

Build the inbox panel, request editing page, and notification flow.

* Add notification `url` field to `notification.py`
* Update Electron `main.js`: right-side `requestsPanelView`, notification click handling, `updateViewBounds`
* Update `preload.js` with new IPC methods
* Update `backend.js` notification handler for click-to-navigate
* Create request-related routes in `app.py`: `/requests/<request_id>`, grant/deny endpoints, `/_chrome/requests-panel`, `/_chrome/requests-events` SSE
* Create templates: requests panel, sharing request editing page
* Update chrome template with requests panel toggle button and badge
* Wire up `is_user_requested` auto-navigation
* Wire up auto-open behavior from `config.toml`
* Handle request grant: execute Cloudflare operations, write response event
* Handle request deny: write response event
* Tests: unit tests for request routes, integration tests for grant/deny flow, notification payload tests

## Testing Strategy

### Unit tests

* `session_store_test.py`: multi-account CRUD, workspace association, token refresh, concurrent access (locking), corrupt file handling
* `minds_config_test.py`: read/write TOML, default values, missing file creation
* `request_events_test.py`: event serialization/deserialization, `RequestInbox` aggregation (add request, add response, dedup by key, pending computation, event replay ordering)
* `backend_resolver_test.py`: updated `ServerLogRecord` with `EventEnvelope` fields, request event routing in `_on_events_stream_output`
* `sharing_proxy_test.py`: verify mutation functions are removed, GET still works
* `cloudflare_client_test.py`: `delete_tunnel` method, per-account tunnel naming

### Integration tests

* Account management: add account, log out, re-login updates tokens, set default
* Workspace association: associate, disassociate with tunnel teardown warning, re-associate
* Request flow end-to-end: workspace server writes request event -> desktop client picks it up -> appears in inbox -> grant -> Cloudflare tunnel created -> response event written -> removed from inbox
* SSE streams: chrome events include request counts, requests-events endpoint streams updates

### Edge cases

* Workspace associated with a logged-out account: sharing operations fail gracefully with a message to re-login
* Duplicate request events: dedup by (agent_id, server_name, request_type) produces single inbox entry
* `sessions.json` corruption: graceful fallback (log warning, treat as empty)
* `config.toml` missing: created with defaults on first write
* Concurrent writes to `sessions.json`: locking prevents corruption
* Right panel toggle while auto-open fires: both paths are idempotent (opening an already-open panel is a no-op)

## Open Questions

* **Tunnel token per account**: Currently tunnel tokens are stored per agent (`~/.minds/agents/<agent_id>/tunnel_token`). With per-workspace-account tunnels, should the token storage be restructured, or is per-agent still correct since each workspace has at most one account?
* **SuperTokens recipe configuration**: The current SuperTokens init in `runner.py` sets up a single `app_info` with one `app_name`. With multi-account, does the SuperTokens SDK need any reconfiguration, or is the same OAuth flow reusable for adding additional accounts (just storing the result differently)?
* **Event file rotation/cleanup**: The event-sourcing model means `~/.minds/events/requests/events.jsonl` grows indefinitely. Should there be a GC mechanism (similar to `gc_logs` in mngr) for old processed request events, or is this deferred?
* **Browser-only mode**: The right-side panel is implemented as an Electron `WebContentsView`. When the minds app is accessed directly from a web browser (not Electron), how should the inbox panel be rendered? The chrome template already has a browser-mode sidebar fallback -- should the requests panel have one too?
