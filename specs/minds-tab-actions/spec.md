# Minds Workspace Server: Tab Action Buttons and Sharing

## Overview

- The dockview tab UI in `minds_workspace_server` currently uses the default dockview tab renderer, which only shows a title and a close ("x") button
- This spec adds a custom tab renderer with up to three action icons per tab: **share** (external-link icon), **destroy** (trash can icon), and **close** (x icon), displayed only on the active/selected tab
- A top-level **share** button is added to the sidebar branding row to control Cloudflare forwarding for the primary agent's "web" server, allowing the entire workspace (including chats) to be shared via a global URL
- A new **destroy** backend endpoint (`POST /api/agents/{agent_id}/destroy`) runs `mngr destroy --force` synchronously
- Three new **sharing proxy** endpoints (`GET/PUT/DELETE /api/sharing/{server_name}`) proxy forwarding requests to the minds desktop client REST API, so the frontend never calls the desktop client directly
- All changes are made in this monorepo at `apps/minds_workspace_server/`; vendoring into `forever-claude-template` happens separately

## Expected Behavior

### Custom tab renderer

- All tabs show only the title when inactive
- The active/selected tab shows the title plus action icons on the right side of the tab header (no hover-triggered display)
- **Close icon** (x): appears on all tab types (chat, iframe, subagent). Removes the panel from the dockview workspace (same behavior as today)
- **Destroy icon** (trash can): appears only on chat/agent tabs. Clicking opens a confirmation dialog. On the primary agent's chat tab, the icon is visible but grayed out/disabled with a tooltip ("Cannot destroy the primary agent")
- **Share icon** (external-link): appears only on iframe/application tabs. Clicking opens the share modal dialog for that application's server name

### Destroy behavior

- The confirmation dialog for a **chat agent** shows the agent name and asks the user to confirm destruction
- The confirmation dialog for a **worktree/sidebar agent** additionally lists all chat children (fetched via `getChatAgentsForParent`) and warns that they will also be destroyed
- The frontend handles cascade: destroys each child chat agent first (sequential `POST /api/agents/{child_id}/destroy` calls), then destroys the parent worktree agent
- After a successful destroy, the tab is explicitly removed from the dockview component
- If the destroyed agent was the currently selected sidebar agent, the UI auto-selects the first remaining sidebar agent (or shows the empty state if none remain)
- `POST /api/agents/{agent_id}/destroy` calls `mngr destroy --force <agent_name>` synchronously and returns `{"status": "ok"}` on success or an error response on failure

### Sidebar share button

- A share icon button appears in the sidebar branding row, next to the hostname text
- Clicking opens the same share modal dialog, but hardcoded to the primary agent's "web" server
- This controls whether the entire workspace is accessible via the Cloudflare-forwarded global URL

### Share modal dialog

- On open, issues `GET /api/sharing/{server_name}` to fetch current forwarding status
- If forwarding is **enabled**: displays the global URL with a "Copy to clipboard" button
- If forwarding is **not enabled**: shows an "Enable sharing" button that issues `PUT /api/sharing/{server_name}`, then refreshes the dialog to show the URL
- If forwarding is **enabled** and the user wants to stop: shows a "Disable sharing" button that issues `DELETE /api/sharing/{server_name}`
- The modal uses the same visual style as the existing `CreateAgentModal` and `custom-url-dialog`

### Sharing proxy endpoints

- `GET /api/sharing/{server_name}`: returns the forwarding status from the desktop client (whether the service is registered and its global hostname)
- `PUT /api/sharing/{server_name}`: enables Cloudflare forwarding for the given server name
- `DELETE /api/sharing/{server_name}`: disables Cloudflare forwarding for the given server name
- All three endpoints read `$MNGR_AGENT_STATE_DIR/minds_api_url` to discover the desktop client API URL, and authenticate using the `MINDS_API_KEY` environment variable as a Bearer token
- No authentication is required on these workspace server endpoints (same trust model as all other workspace server endpoints)
- If the `minds_api_url` file does not exist or `MINDS_API_KEY` is not set, the endpoints return an appropriate error

## Implementation Plan

### Backend (`imbue/minds_workspace_server/`)

**`server.py`** -- add three new routes:
- `POST /api/agents/{agent_id}/destroy` -> `_destroy_agent()`: looks up the agent by ID, runs `mngr destroy --force <agent_name>` via `run_local_command_modern_version`, returns success/failure JSON
- `GET /api/sharing/{server_name}` -> `_get_sharing_status()`: reads `minds_api_url` file, calls `GET /api/v1/agents/{own_agent_id}/servers/{server_name}/cloudflare` on the desktop client (or uses `list_services` equivalent), returns forwarding status
- `PUT /api/sharing/{server_name}` -> `_enable_sharing()`: reads `minds_api_url` file, calls `PUT /api/v1/agents/{own_agent_id}/servers/{server_name}/cloudflare` on the desktop client, returns result
- `DELETE /api/sharing/{server_name}` -> `_disable_sharing()`: reads `minds_api_url` file, calls `DELETE /api/v1/agents/{own_agent_id}/servers/{server_name}/cloudflare` on the desktop client, returns result

**`sharing_proxy.py`** (new file) -- helper module for minds desktop client communication:
- `read_minds_api_url() -> str | None`: reads `$MNGR_AGENT_STATE_DIR/minds_api_url` and returns the URL, or None
- `get_sharing_status(server_name: str) -> dict`: issues GET to the desktop client API, returns `{"enabled": bool, "url": str | None}`
- `enable_sharing(server_name: str) -> dict`: issues PUT to the desktop client API
- `disable_sharing(server_name: str) -> dict`: issues DELETE to the desktop client API
- All functions use `MINDS_API_KEY` env var for Bearer token auth and `MNGR_AGENT_ID` env var for the agent ID path parameter
- Uses `httpx` (already a dependency) for HTTP requests

**`models.py`** -- add response models:
- `DestroyAgentResponse`: `{"status": str}`
- `SharingStatusResponse`: response from desktop client forwarding API

### Frontend (`frontend/src/`)

**`views/DockviewWorkspace.ts`** -- major changes:
- Add `createTabComponent` option to the `DockviewComponent` constructor to provide a custom tab renderer
- The custom tab renderer creates an HTML element with: the panel title (span), and a button group (share, destroy, close icons as inline SVG buttons)
- Share button: visible only if `panelType === "iframe"`. `onclick` opens the share modal with the panel's `title` (application name) as the server name
- Destroy button: visible only if `panelType === "chat"`. Disabled if `chatAgentId` matches the primary agent ID (`MNGR_AGENT_ID`). `onclick` opens the destroy confirmation dialog
- Close button: visible on all panel types. `onclick` calls `dockview.api.removePanel(panel)`
- Only the active panel's tab shows the action buttons; inactive tabs show only the title
- Listen to `onDidActivePanelChange` to update which tab shows action buttons (re-render tab elements)

**`views/ShareModal.ts`** (new file) -- share modal dialog component:
- Mithril component accepting `serverName: string` and `onClose: () => void` as attrs
- On mount, fetches `GET /api/sharing/{serverName}` via `apiUrl()`
- Displays loading state, then the result:
  - Enabled: global URL text + "Copy" button (uses `navigator.clipboard.writeText`) + "Disable sharing" button
  - Not enabled: "Enable sharing" button
- Enable/disable buttons issue PUT/DELETE and re-fetch status
- Styled consistently with existing dialogs (overlay + centered card)

**`views/DestroyConfirmDialog.ts`** (new file) -- destroy confirmation dialog component:
- Mithril component accepting `agentId: string`, `agentName: string`, `chatChildren: AgentState[]`, `isPrimary: boolean`, `onConfirm: () => void`, `onCancel: () => void`
- For chat agents (no children): "Are you sure you want to destroy {name}? This cannot be undone."
- For worktree agents (with children): "Destroying {name} will also destroy these chat agents: {list}. This cannot be undone."
- "Destroy" button (red/destructive styling) and "Cancel" button
- The calling code in DockviewWorkspace handles the actual destroy API calls and cascade logic

**`views/Sidebar.ts`** -- add share button:
- Add a share icon button in the `sidebar-branding-row` div, next to the hostname and collapse button
- `onclick` opens the `ShareModal` with `serverName = "web"`
- Uses the same inline SVG helper pattern as existing sidebar icon buttons

**`models/AgentManager.ts`** -- add helper:
- Export `getPrimaryAgentId(): string` that returns the primary agent ID (read from a meta tag or passed from server, similar to `getHostname()` in `base-path.ts`)

**`base-path.ts`** or new module -- expose primary agent ID:
- The workspace server injects a `<meta name="minds-workspace-server-agent-id">` tag into `index.html` (like the existing `minds-workspace-server-hostname` meta tag)
- Frontend reads this to determine which agent is the primary/host agent

### CSS (`frontend/src/style.css`)

- Add styles for the custom tab renderer button group (`.dv-custom-tab-actions`)
- Action buttons: small, icon-only, matching existing dockview tab styling (12-16px icons, subtle colors, no background)
- Disabled state for the destroy button on primary agent (opacity reduction, cursor: not-allowed)
- Share modal and destroy dialog styles (overlay, card, buttons) -- follow existing `custom-url-dialog` pattern
- Sidebar share button style (same as `sidebar-inline-icon-button`)

## Implementation Phases

### Phase 1: Backend endpoints

- Add `sharing_proxy.py` with minds desktop client communication helpers
- Add `POST /api/agents/{agent_id}/destroy` endpoint to `server.py`
- Add `GET/PUT/DELETE /api/sharing/{server_name}` endpoints to `server.py`
- Add response models to `models.py`
- Inject `MNGR_AGENT_ID` as a meta tag into the HTML response
- Result: backend is fully functional, testable via curl

### Phase 2: Custom tab renderer

- Add `createTabComponent` to `DockviewWorkspace.ts` with close button only (matching current behavior)
- Add active/inactive tab logic (action buttons only on active tab)
- Add CSS for the custom tab button group
- Result: tabs look and behave the same as before, but using the custom renderer

### Phase 3: Destroy functionality

- Add `DestroyConfirmDialog.ts` component
- Wire destroy icon in custom tab renderer to open the dialog (chat tabs only, disabled on primary agent)
- Add primary agent ID discovery (meta tag + frontend reader)
- Implement cascade logic: destroy children, then parent, then remove tab and auto-select next agent
- Result: agents can be destroyed from the tab UI

### Phase 4: Share functionality

- Add `ShareModal.ts` component
- Wire share icon in custom tab renderer to open the modal (iframe tabs only)
- Add sidebar share button in branding row for the primary agent's "web" server
- Result: sharing is fully functional

## Testing Strategy

### Unit tests (backend)

- `sharing_proxy.py`: test `read_minds_api_url` with missing file, empty file, valid file; test each proxy function with mocked HTTP responses (success, failure, missing env vars)
- `server.py` destroy endpoint: test with valid agent ID (mock `run_local_command_modern_version`), unknown agent ID, command failure
- `server.py` sharing endpoints: test proxy behavior with mocked `sharing_proxy` functions

### Integration tests (backend)

- Create a test agent via the existing `create-chat` endpoint, then destroy it via the new destroy endpoint, verify it disappears from the agent list
- Test sharing endpoints with a mock desktop client API server

### Frontend (manual verification via tmux)

- Verify the custom tab renderer shows the correct icons per tab type (chat: destroy + close; iframe: share + close; subagent: close only)
- Verify inactive tabs show only the title, active tab shows title + actions
- Verify the destroy confirmation dialog lists chat children for worktree agents
- Verify the share modal fetches and displays forwarding status correctly
- Verify the sidebar share button opens the share modal for the "web" server
- Verify the primary agent's destroy button is disabled

### Edge cases

- Destroy an agent that is already gone (race condition) -- should handle gracefully
- Share when `minds_api_url` file doesn't exist -- should show a clear error in the modal
- Share when `MINDS_API_KEY` is not set -- should show a clear error
- Destroy the currently selected sidebar agent -- should auto-select the next agent
- Destroy a worktree agent whose chat children have already been destroyed externally -- should handle missing children gracefully during cascade

## Open Questions

- The exact response format from the desktop client's `list_services` endpoint may vary; the sharing proxy should handle both the per-service response and the full services list response gracefully
- Whether the `mngr destroy --force` command should run with a timeout to prevent the endpoint from hanging if destruction takes unexpectedly long
- Whether the frontend should poll or use the existing WebSocket `agents_updated` event to detect when a destroyed agent has actually been removed from the agent list (relevant for sidebar auto-selection timing)
