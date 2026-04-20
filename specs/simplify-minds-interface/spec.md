# Simplify Minds Workspace Interface

## Overview

* The minds_workspace_server UI currently has three separate concepts surfaced through different UI affordances: a left sidebar listing "agents" (worktrees), a "+" tab button with grouped "Chat" and "Applications" sections, and parent-child relationships between agents via `chat_parent_id` labels. This is too much complexity for the target user.
* This spec flattens the entire interface into a single shared dockview with one unified "+" dropdown menu. There is no longer a "selected agent" concept -- every agent, chat, application, and terminal is just a tab.
* The left sidebar is fully removed (code and styles deleted). Agents that were previously listed there now appear in the "+" dropdown alongside chats and applications.
* The `chat_parent_id` parent-child relationship is removed. All agents and chats are peers in a flat list.
* Applications are simplified to primary-agent-only (no per-agent application tracking). The backend watches only the primary agent's `runtime/applications.toml`.
* The workspace "share" button (which was in the sidebar) is removed. Per-tab share buttons on iframe tabs remain.
* Plugin sidebar items (`sidebar-items.ts`) are dropped for now.

## Expected Behavior

### "+" dropdown menu structure

* The "+" button in the dockview tab bar opens a flat dropdown with this structure:
  * **Existing items section** (top): Any agents, chats, or applications that exist but do not currently have a tab open in the dockview. Listed by name with no type distinction (icons to be added later). Applications listed here exclude "web" and "terminal".
  * **Divider** (only if existing items section is non-empty)
  * `New chat` -- creates a standalone chat agent (same work dir as primary, chat template, no `chat_parent_id`)
  * `New terminal` -- opens a new terminal iframe tab pointing to the primary agent's ttyd/work directory
  * `New URL` -- opens the custom URL dialog (same as current "+ custom URL")
  * `New agent` -- opens the CreateAgentModal in worktree mode to create a new git worktree agent
* Clicking an existing item in the "..." section opens a tab for it (chat tab for agents/chats, iframe tab for applications)

### Single shared dockview

* There is exactly one `DockviewComponent` instance for the entire workspace (not one per agent as before)
* All chat tabs, terminal tabs, iframe tabs, and subagent tabs coexist in this single dockview
* Tab titles show the agent/chat name, application name, or "terminal"
* The default initial state (no saved layout) is a single tab: the primary agent's chat

### Agent/chat tab behavior

* All agent and chat tabs (except the primary agent) have a destroy button in the tab actions
* Destroying an agent removes the tab and calls the destroy endpoint
* Subagent tabs continue to work as before -- opened from within chat panels as additional tabs in the shared dockview

### Layout persistence

* Layout is saved as a single global layout keyed by the primary agent ID + access mode (cloudflare, local, dev)
* The save/load endpoints use the primary agent ID with a `mode` query parameter (same pattern as before but one layout instead of per-agent)

### URL routing

* The `/agents/:agentId/` route is removed
* The app serves on a single static route (e.g. `/`)
* No agent ID in the URL

### Agent creation flow

* "New agent" opens the `CreateAgentModal` in worktree mode (same as the old sidebar "+" button)
* "New chat" opens the `CreateAgentModal` in chat mode but without setting `parent_agent_id`
* When creation starts, a tab immediately opens showing the creation/loading progress (same as current chat creation behavior)
* Proto-agents (creating...) appear in the "+" dropdown's existing items section if they don't already have a tab

### Terminal behavior

* "New terminal" always opens in the primary agent's work directory
* Each click opens a new terminal tab (multiple terminals allowed)
* Terminal URL routes through the primary agent's ttyd instance

### Applications

* Only the primary agent's applications are tracked
* The backend watches only the primary agent's `runtime/applications.toml`
* The WebSocket `applications_updated` event broadcasts a flat list (not a per-agent map)
* Applications (excluding "web" and "terminal") that don't have an open tab appear in the "+" dropdown's existing items section

### What is removed

* Left sidebar (`Sidebar.ts`, `ConversationSelector.ts`, associated CSS)
* Sidebar items plugin system (`sidebar-items.ts`, `registerSidebarItem`)
* Sidebar share button (workspace-level share via "web" server name)
* "Selected agent" concept and per-agent dockview switching
* Per-agent routing (`/agents/:agentId/`)
* `chat_parent_id` label on chat creation
* Per-agent application watching (backend only watches primary agent)
* Per-agent application map in WebSocket broadcasts

## Implementation Plan

### Frontend changes

* **Delete `frontend/src/views/Sidebar.ts`** -- remove the entire sidebar component
* **Delete `frontend/src/views/ConversationSelector.ts`** -- agent list no longer rendered in a sidebar
* **Delete `frontend/src/sidebar-items.ts`** -- plugin sidebar items dropped
* **Modify `frontend/src/views/App.ts`**:
  * Remove `Sidebar` import and rendering
  * Remove `selectedAgentId` concept -- just render `DockviewWorkspace` without an `agentId` prop
  * The layout becomes just the dockview filling the full width
* **Rewrite `frontend/src/models/AgentManager.ts`**:
  * Remove `getSidebarAgents()` (no sidebar)
  * Remove `getChatAgentsForParent()` and `getChatProtoAgentsForParent()` (no parent-child)
  * Change `applications` from `Record<string, ApplicationEntry[]>` to `ApplicationEntry[]` (flat list)
  * Update `getApplicationsForAgent()` to just return the flat applications list (rename to `getApplications()`)
  * Remove auto-select-first-agent logic from `agents_updated` handler
  * Add `getProtoAgentById(id)` helper for checking if a proto-agent already has a tab
  * Handle `applications_updated` event with new flat list shape
* **Remove `frontend/src/navigation.ts`** -- no more agent routing
  * Remove `getSelectedAgentId()` and `selectAgent()` functions
  * Update all imports that reference these
* **Rewrite `frontend/src/views/DockviewWorkspace.ts`**:
  * Change from managing a `Map<agentId, AgentDockviewState>` to a single `DockviewComponent`
  * Remove `agentId` prop from the component
  * Remove `currentAgentId`, `agentDockviews` map, `showAgentDockview()`, `initializeAgentDockview()`
  * Single `createDockview()` on mount that creates one dockview
  * Rewrite `buildDropdownItems()` to produce the new flat menu:
    * Query all agents (`getAgents()`) and all proto-agents to find those without open tabs
    * Query applications (`getApplications()`) to find apps (excluding web/terminal) without open tabs
    * Add "New chat", "New terminal", "New URL", "New agent" items
  * Update `addChatPanel()` to work without an "owner" agent context
  * Terminal URL always uses `getTerminalUrl()` with primary agent's work_dir
  * Layout save/load uses primary agent ID + mode as key
  * On initial load (no saved layout), open primary agent's chat tab
  * All agent/chat tabs (except primary) get destroy button
  * Tab tracking: maintain a set of open panel IDs to determine which items are "un-tabbed" for the dropdown
* **Modify `frontend/src/views/CreateAgentModal.ts`**:
  * Chat mode: remove `parentAgentId` prop requirement, stop sending `parent_agent_id` in the request
  * Use the primary agent ID (from `getPrimaryAgentId()`) as the `selected_agent_id` for worktree creation
* **Update `frontend/src/index.ts`** (or equivalent entry point):
  * Remove Mithril route for `/agents/:agentId/`
  * Set up a single route at `/` or `#!/`
* **Update CSS**:
  * Delete sidebar-related styles (`.app-sidebar`, `.sidebar-*`, `.conversation-selector-*`)
  * Adjust `.app-layout` to remove sidebar flex child

### Backend changes

* **Modify `agent_manager.py`**:
  * `_initial_discover()`: only call `_start_app_watcher()` for the primary agent (identified by `self._own_agent_id`)
  * `_handle_full_snapshot()`, `_handle_agent_discovered()`: only start app watchers for the primary agent
  * `get_applications()` / `get_applications_serialized()`: return flat list instead of per-agent map
  * `broadcast_applications_updated()`: send flat list shape
  * `create_chat_agent()`: remove `--label chat_parent_id=...` from the mngr create command, remove `chat_parent_id` from the labels dict
* **Modify `ws_broadcaster.py`**:
  * `broadcast_applications_updated()`: change signature from `dict[str, list[dict[str, str]]]` to `list[dict[str, str]]`
* **Modify `models.py`**:
  * `CreateChatRequest`: remove `parent_agent_id` field (or make it optional/unused)
* **Modify `server.py`**:
  * Update the `create-chat` endpoint to not require `parent_agent_id`
  * Update the WebSocket handler's initial state push to send flat application list
  * Update layout save/load to support a global layout key (or reuse primary agent ID)
  * Remove or simplify the agent route if backend serves the SPA

## Implementation Phases

### Phase 1: Backend simplification

* Modify `agent_manager.py` to only watch primary agent's applications
* Change application data structures from per-agent map to flat list
* Update `ws_broadcaster.py` signature for flat application list
* Remove `chat_parent_id` from chat agent creation
* Update `CreateChatRequest` model to drop `parent_agent_id`
* Update `server.py` create-chat endpoint
* All existing tests should still pass (or be updated for new signatures)

### Phase 2: Frontend -- remove sidebar and flatten navigation

* Delete `Sidebar.ts`, `ConversationSelector.ts`, `sidebar-items.ts`
* Update `App.ts` to remove sidebar, render dockview full-width
* Remove `navigation.ts` (selected agent routing)
* Update `AgentManager.ts` to remove parent-child helpers and change application shape to flat list
* Update entry point routing to single static route
* Delete sidebar CSS

### Phase 3: Frontend -- rewrite dockview to single instance

* Rewrite `DockviewWorkspace.ts` to use a single `DockviewComponent`
* Implement the new `buildDropdownItems()` with flat menu structure
* Implement tab tracking (which panels are open) to determine "un-tabbed" items for the dropdown
* Update layout save/load to use primary agent ID + mode
* Default initial state: primary agent's chat tab
* Update `CreateAgentModal.ts` to remove parent agent dependency for chat mode

### Phase 4: Frontend -- creation flow and destroy

* Wire "New agent" and "New chat" to open a tab immediately on creation
* Show proto-agent (creating...) entries in the dropdown for agents without tabs
* Add destroy button to all agent/chat tabs except primary
* Test destroy cascading (removing tab, calling API, updating state)

## Testing Strategy

### Unit tests

* `AgentManager` (backend): verify that application watchers are only started for the primary agent
* `AgentManager` (backend): verify `create_chat_agent` no longer sets `chat_parent_id` label
* `AgentManager` (backend): verify `get_applications_serialized()` returns flat list
* `WebSocketBroadcaster`: verify `broadcast_applications_updated` sends flat list shape
* `CreateChatRequest` model: verify it works without `parent_agent_id`

### Integration tests

* WebSocket connection receives `applications_updated` with flat list (not per-agent map)
* `POST /api/agents/create-chat` works without `parent_agent_id` in request body
* Layout save/load works with global key (primary agent ID + mode)
* Agent destroy endpoint still works correctly

### Manual verification

* Open workspace -- no sidebar, full-width dockview with primary agent chat tab
* Click "+" -- see flat menu with "New chat", "New terminal", "New URL", "New agent"
* Create a new chat -- tab opens immediately with creation progress, then shows chat
* Create a new agent (worktree) -- tab opens immediately with creation progress
* Close a chat/agent tab -- it appears in the "+" dropdown's existing items section
* Click an existing item in the dropdown -- tab reopens
* Create a terminal -- opens in primary agent's work directory
* Create multiple terminals -- all open as separate tabs
* Destroy a chat/agent tab -- tab removed, agent destroyed
* Applications from primary agent appear in dropdown when not tabbed
* Layout persists across page refresh (same mode)
* Subagent tabs still open from chat panels
* Per-tab share buttons on iframe tabs still work

### Edge cases

* No agents exist except primary -- dropdown shows only "New ..." items
* All agents/chats have open tabs -- no existing items section in dropdown
* Agent creation fails -- error handling, proto-agent cleaned up
* WebSocket reconnection -- state rebuilds correctly with flat application list
* Cloudflare mode vs local mode -- layout saved/loaded per mode correctly

## Open Questions

* How should the workspace handle deep-linking or bookmarking now that there's no agent ID in the URL? (Currently deferred -- single static route is the plan)
* Should the "share workspace" functionality return in a different form later? (User noted "we'll come back to this later")
* Will persistent terminal instances (reusable across sessions) be needed? (User noted "we'll come back to this later in order to have more persistent instances")
* Plugin sidebar items are dropped -- should a plugin hook for the "+" dropdown be added in the future?
