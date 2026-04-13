# Service Tabs and Agent Creation -- Spec

## Overview

- The minds workspace server currently has hard-coded "Chat", "Terminal", and "Custom URL" options in the dockview "+" tab menu. We want to replace these with dynamic options: per-agent chat entries, applications from `runtime/applications.toml`, and "Custom URL."
- There is currently only a single agent per workspace. We want to support creating two types of new agents: **worktree agents** (new git worktree, new branch, separate work dir) and **chat agents** (same work dir, just another Claude session).
- A new unified WebSocket from the workspace server pushes all dynamic state (agent list, applications, proto-agent lifecycle) to the frontend. This replaces the current one-shot `GET /api/agents` fetch.
- The workspace server handles all agent creation directly via local `mngr create`, preserving isolation from the user's keys and data on the desktop client.
- Proto-agents (agents being created) are tracked separately from real agents. Creation logs stream via a per-proto-agent WebSocket, similar to existing chat event streaming.
- New "chat" and "worktree" create templates are added to the forever-claude-template repo. The "chat" template is minimal (just Claude, no services). The "worktree" template starts with no services -- the agent creates and runs its own.
- The desktop client's `agent_creator.py` is updated to pass `--label user_created=true` when creating workspace agents.

## Expected Behavior

### Sidebar (left panel)

- Shows all agents on the host that do **not** have a `chat_parent_id` label. This includes the original main agent and any worktree agents.
- All agents are styled equally (no visual distinction between main and worktree agents). Sorted by creation time or alphabetically.
- A "+" button next to the "Agents" header opens a creation modal for worktree agents.
- Clicking the "+" opens a modal with a single "Name" field, pre-filled with a random adjective-noun name (via mngr's `generate_agent_name` / coolname).
- On confirm, a `POST /api/agents/create-worktree` request is sent with `{name: string}`. The response includes the pre-generated `agent_id`.
- A `proto_agent_created` event appears on the unified WebSocket. The proto-agent shows in the sidebar with a "creating" state indicator.
- When the proto-agent is selected in the sidebar, the main panel shows a log viewer. A WebSocket connection opens to `ws://.../api/proto-agents/{id}/logs` and streams creation output. The first line is the exact `mngr create` invocation with full directory path.
- When creation completes, a `proto_agent_completed` event fires on the unified WebSocket. The frontend closes the log WebSocket and redirects to the chat view for the new agent. The next `agents_updated` event includes the real agent.

### "+" tab menu (dockview header)

- The menu is a flat list with visual dividers, organized in this order:
  1. **Chat entries**: "Chat (\<agent-name\>)" for the selected sidebar agent itself, plus one entry for each agent with `chat_parent_id=<selected-agent-id>`. Clicking opens or focuses that agent's chat tab (max 1 chat tab per agent).
  2. **"New Chat"**: Opens a creation modal (same as worktree: name field with random default). On confirm, `POST /api/agents/create-chat` with `{name: string, parent_agent_id: string}`. Proto-agent appears as a tab showing creation logs. On completion, transitions to a chat tab.
  3. **Applications**: One entry per application from the selected sidebar agent's `runtime/applications.toml`. Clicking opens an iframe tab pointed at the forwarding server's proxied URL: `/agents/{agent_id}/{server_name}/`. Dynamically updated via the unified WebSocket.
  4. **"Custom URL"**: Opens the existing URL input dialog (unchanged).
- The hard-coded "Terminal" option is removed. Terminal appears only if registered as an application in `runtime/applications.toml`.

### Chat tabs

- All chat tabs are closable (the current unclosable singleton behavior is removed).
- Panel IDs follow the format `chat-{chatAgentId}` to support multiple chat panels per dockview.
- Closing a chat tab does not destroy the agent. The tab can be reopened from the "+" menu.
- At most 1 chat tab per agent can be open. Selecting "Chat (\<agent-name\>)" when the tab is already open focuses it instead of creating a duplicate.
- Layout persistence saves which chat tabs are open and restores them.

### Application tabs

- Each application entry in the "+" menu shows the application name (e.g., "web", "terminal").
- Clicking opens an iframe tab with the proxied URL `/agents/{selected_agent_id}/{app_name}/`.
- Applications are dynamically updated: if a new service registers itself in `runtime/applications.toml` while the workspace is running, it appears in the menu without page reload.

### Unified WebSocket

- Endpoint: `ws://.../api/ws`
- On connection, sends a full snapshot: one `agents_updated` message and one `applications_updated` message.
- Thereafter, sends incremental updates as changes are detected.
- JSON-lines format: one JSON object per message, discriminated by `type` field.
- Event types:
  - `agents_updated`: `{type: "agents_updated", agents: [{id, name, state, labels, work_dir}, ...]}` -- full agent list snapshot (agents without `chat_parent_id` for sidebar, agents with `chat_parent_id` for tab menu grouping)
  - `applications_updated`: `{type: "applications_updated", applications: {agent_id: [{name, url}, ...], ...}}` -- per-agent application map
  - `proto_agent_created`: `{type: "proto_agent_created", agent_id: string, name: string, creation_type: "worktree" | "chat", parent_agent_id: string | null}`
  - `proto_agent_completed`: `{type: "proto_agent_completed", agent_id: string, success: boolean, error: string | null}`

### Proto-agent log WebSocket

- Endpoint: `ws://.../api/proto-agents/{id}/logs`
- Streams creation output line-by-line as JSON: `{line: string}`
- First line is the exact `mngr create` command with full working directory path.
- On completion, sends `{done: true, success: boolean, error: string | null}` and closes.
- Frontend opens this WebSocket when a proto-agent is selected, closes it when navigating away.

### Agent creation commands

- **Worktree agent**: `mngr create <name> --id <pre-generated-id> --transfer git-worktree --branch <current-branch>:mngr/<name> --template worktree --label user_created=true --no-connect` run from the selected sidebar agent's work directory.
- **Chat agent**: `mngr create <name> --id <pre-generated-id> --transfer none --template chat --label chat_parent_id=<selected-sidebar-agent-id> --no-connect` run from the selected sidebar agent's work directory.
- The workspace server pre-generates the agent ID (using mngr's `AgentId()`) and returns it from the POST endpoint. This ID is used for the proto-agent log WebSocket and the eventual chat redirect.

### Template changes (forever-claude-template)

- **"chat" create template** in `.mngr/settings.toml`: type "claude", no extra_window, no env overrides, no services. Minimal -- just the Claude agent process.
- **"worktree" create template** in `.mngr/settings.toml`: type "main", no extra_window, no services by default. The agent creates and runs its own services as needed.
- The desktop client's `agent_creator.py` passes `--label user_created=true` alongside `--label workspace=<name>` when creating workspace agents (no mngr template system changes needed).

### Backend detection mechanisms

- The workspace server runs `mngr observe --discovery-only --events-dir $MNGR_AGENT_STATE_DIR/workspace_server/observe/` in the background. The custom `--events-dir` avoids conflicts with the desktop client's own `mngr observe` in dev mode.
- Agent lifecycle events (created, destroyed, state changes) from `mngr observe` trigger `agents_updated` WebSocket messages.
- The workspace server reads `work_dir` from `DiscoveredAgent` discovery events (available via the `work_dir` property on the `DiscoveredAgent` model).
- For each discovered agent with a `work_dir`, the workspace server watches `<work_dir>/runtime/applications.toml` for changes (via filesystem events). Changes trigger `applications_updated` WebSocket messages.
- The workspace server reads `MNGR_AGENT_ID` and `MNGR_AGENT_WORK_DIR` from its own environment for self-awareness.

### REST API changes

- `DELETE /api/agents` endpoint (the existing `GET /api/agents` endpoint is removed; the WebSocket is the single source of truth for agent state).
- `POST /api/agents/create-worktree`: body `{name: string}`, returns `{agent_id: string}`. The workspace server determines the selected agent's work_dir from its tracked agent state.
- `POST /api/agents/create-chat`: body `{name: string, parent_agent_id: string}`, returns `{agent_id: string}`. The `parent_agent_id` identifies which sidebar agent this chat belongs to.
- `GET /api/agents/{agent_id}/layout` and `POST /api/agents/{agent_id}/layout`: unchanged.
- Chat event SSE endpoints (`/api/agents/{agent_id}/stream`, etc.): unchanged (keep SSE; WebSocket conversion is future work).

## Implementation Plan

### Backend changes (workspace server)

**New file: `apps/minds_workspace_server/imbue/minds_workspace_server/agent_manager.py`**
- `AgentManager` class: manages the lifecycle of `mngr observe`, agent tracking, application watching, and agent creation.
- `start()`: launches `mngr observe --discovery-only --events-dir <custom-dir>` as a background subprocess. Parses JSONL output and updates internal agent state.
- `stop()`: terminates the `mngr observe` subprocess and cleans up file watchers.
- `get_agents() -> list[AgentState]`: returns current agent list with id, name, state, labels, work_dir.
- `get_applications() -> dict[str, list[ApplicationEntry]]`: returns per-agent application map.
- `create_worktree_agent(name: str) -> AgentId`: pre-generates ID, resolves selected agent's work_dir and current branch (via `git -C <work_dir> branch --show-current`), spawns `mngr create` in background thread, returns ID.
- `create_chat_agent(name: str, parent_agent_id: str) -> AgentId`: pre-generates ID, resolves parent agent's work_dir, spawns `mngr create` in background thread, returns ID.
- `get_proto_agent_log_queue(agent_id: str) -> asyncio.Queue[str] | None`: returns the log queue for a proto-agent creation process.
- Internal: `_on_agent_discovered(agent: DiscoveredAgent)`, `_on_agent_destroyed(agent_id: str)` -- update agent state and start/stop application file watchers.
- Internal: `_watch_applications(agent_id: str, work_dir: Path)` -- uses `watchdog` (already a dependency) to monitor `runtime/applications.toml` changes. Parses TOML on change.
- Internal: `_on_applications_changed(agent_id: str)` -- reads `runtime/applications.toml`, updates state, notifies WebSocket broadcaster.
- Uses `generate_agent_name(AgentNameStyle.COOLNAME)` from `imbue.mngr.utils.name_generator` for random default names (exposed via a helper endpoint or returned to frontend as part of the creation flow).

**New file: `apps/minds_workspace_server/imbue/minds_workspace_server/ws_broadcaster.py`**
- `WebSocketBroadcaster` class: manages connected WebSocket clients and broadcasts events.
- `connect(websocket: WebSocket)`: accepts connection, sends initial snapshot (agents_updated + applications_updated), adds to client set.
- `disconnect(websocket: WebSocket)`: removes from client set.
- `broadcast(message: dict)`: serializes as JSON line and sends to all connected clients.
- `broadcast_agents_updated(agents: list[AgentState])`: constructs and broadcasts `agents_updated` message.
- `broadcast_applications_updated(applications: dict)`: constructs and broadcasts `applications_updated` message.
- `broadcast_proto_agent_created(agent_id: str, name: str, creation_type: str, parent_agent_id: str | None)`.
- `broadcast_proto_agent_completed(agent_id: str, success: bool, error: str | None)`.

**Modified file: `apps/minds_workspace_server/imbue/minds_workspace_server/server.py`**
- Add WebSocket endpoint `GET /api/ws` (upgraded to WebSocket) that delegates to `WebSocketBroadcaster`.
- Add WebSocket endpoint `GET /api/proto-agents/{agent_id}/logs` that streams from `AgentManager.get_proto_agent_log_queue()`.
- Add `POST /api/agents/create-worktree` route: validates name, calls `AgentManager.create_worktree_agent()`, returns `{agent_id}`.
- Add `POST /api/agents/create-chat` route: validates name and parent_agent_id, calls `AgentManager.create_chat_agent()`, returns `{agent_id}`.
- Add `GET /api/random-name` route: returns `{name: string}` using mngr's name generator (for pre-filling the creation modal).
- Remove `GET /api/agents` route (replaced by WebSocket).
- Update lifespan to initialize `AgentManager` and `WebSocketBroadcaster` on startup, clean up on shutdown.
- Wire `AgentManager` callbacks to `WebSocketBroadcaster` methods.

**Modified file: `apps/minds_workspace_server/imbue/minds_workspace_server/agent_discovery.py`**
- Keep `discover_agents()` and `send_message()` for use by session watchers and message sending (chat events still use the existing SSE infrastructure).
- Add `get_agent_labels(agent_id: str) -> dict[str, str]` helper if needed by session watchers.

**Modified file: `apps/minds_workspace_server/imbue/minds_workspace_server/models.py`**
- Add `AgentState` model: `{id: str, name: str, state: str, labels: dict[str, str], work_dir: str | None}`.
- Add `ApplicationEntry` model: `{name: str, url: str}`.
- Add `CreateWorktreeRequest` model: `{name: str}`.
- Add `CreateChatRequest` model: `{name: str, parent_agent_id: str}`.
- Add `CreateAgentResponse` model: `{agent_id: str}`.
- Add `ProtoAgentInfo` model: `{agent_id: str, name: str, creation_type: str, parent_agent_id: str | None}`.

**Modified file: `apps/minds_workspace_server/imbue/minds_workspace_server/main.py`**
- No changes to CLI flags (agent filtering still works via `--include`/`--exclude`/`--provider`).
- The `AgentManager` uses these same filters when interpreting `mngr observe` output.

### Frontend changes (workspace server)

**New file: `frontend/src/models/AgentManager.ts`**
- Manages the unified WebSocket connection to `/api/ws`.
- On connection, receives initial snapshot and populates state.
- On `agents_updated`: updates `agents` array (used by sidebar and tab menu).
- On `applications_updated`: updates `applications` map (used by tab menu).
- On `proto_agent_created`: adds to `protoAgents` array.
- On `proto_agent_completed`: removes from `protoAgents`, triggers redraw.
- Exports: `getAgents()`, `getAgentById(id)`, `getApplicationsForAgent(id)`, `getProtoAgents()`, `getChatAgentsForParent(parentId)`.
- Auto-reconnects on WebSocket close (with backoff).

**New file: `frontend/src/views/ProtoAgentLogView.ts`**
- Mithril component that opens a WebSocket to `/api/proto-agents/{id}/logs` on mount.
- Displays streaming log lines in a terminal-like scrolling view.
- On `{done: true}`, closes WebSocket and triggers navigation to the chat view for that agent.
- On unmount (navigate away), closes the WebSocket.

**New file: `frontend/src/views/CreateAgentModal.ts`**
- Mithril component rendering a modal overlay with a single "Name" input field.
- Pre-fills with a random name fetched from `GET /api/random-name`.
- On confirm, calls the appropriate creation endpoint and closes.
- On cancel / Escape / overlay click, closes without action.
- Used by both worktree and chat agent creation flows.

**Modified file: `frontend/src/views/DockviewWorkspace.ts`**
- Replace `createAddTabButton()` dropdown:
  - Remove hard-coded "Chat" and "Terminal" entries.
  - Dynamically build chat entries from `AgentManager.getChatAgentsForParent(selectedAgentId)` plus the selected agent itself. Format: "Chat (\<name\>)".
  - Add "New Chat" entry that opens `CreateAgentModal` in chat mode.
  - Add application entries from `AgentManager.getApplicationsForAgent(selectedAgentId)`. Each opens an iframe at `/agents/{selectedAgentId}/{appName}/`.
  - Add "Custom URL" entry (existing behavior).
  - Add visual dividers (`<hr>` or CSS borders) between sections.
- Change `addChatPanel()`:
  - Accept `chatAgentId` parameter instead of using the dockview's own agent ID.
  - Panel ID: `chat-{chatAgentId}`.
  - All chat panels are closable (remove the custom `createTabComponent` that suppresses the close button).
- Change `focusOrCreateChatPanel()` to `focusOrCreateChatPanelForAgent(agentId, chatAgentId)`:
  - Looks for existing panel with ID `chat-{chatAgentId}`.
  - If found, focuses it. If not, creates a new chat panel for that agent.
- Add `addProtoAgentPanel(agentId, protoAgentId, name)`:
  - Creates a panel with type "proto-agent" showing `ProtoAgentLogView`.
  - On creation completion, replaces with chat panel for that agent.
- Update `createComponent()` to handle "proto-agent" panel type.
- Update layout save/restore to handle variable chat panel IDs.

**Modified file: `frontend/src/views/Sidebar.ts`**
- The "+" button next to "Agents" header calls `CreateAgentModal` in worktree mode.
- On confirm, `POST /api/agents/create-worktree`. The proto-agent immediately appears in the agent list (from the WebSocket event).

**Modified file: `frontend/src/views/ConversationSelector.ts`**
- Replace `fetchAgents()` call with reading from `AgentManager.getAgents()`.
- Filter display to agents without `chat_parent_id` label (sidebar agents).
- Include proto-agents (from `AgentManager.getProtoAgents()`) with a "creating" state badge.
- Clicking a proto-agent navigates to it, showing the log viewer in the main panel.

**Modified file: `frontend/src/models/Conversation.ts`**
- Remove `fetchAgents()` and direct API calls. Agent state now comes from `AgentManager`.
- Keep `fetchConversations` shim for plugin compatibility if needed.

**Modified file: `frontend/src/models/StreamingMessage.ts`**
- No changes (chat SSE streaming remains as-is).

**Modified file: `frontend/src/views/ChatPanel.ts`**
- Accept a `chatAgentId` parameter (the agent whose chat to display) in addition to the dockview's `agentId`.
- Use `chatAgentId` for SSE connection and message sending.

### Template changes (forever-claude-template via `.external_worktrees/`)

**Modified file: `.mngr/settings.toml`**
- Add `[create_template.chat]` section:
  - `type = "claude"`
  - No `extra_window`, no `env` overrides.
- Add `[create_template.worktree]` section:
  - `type = "main"`
  - No `extra_window`, no `env` overrides, no services.

### Desktop client changes (minds app)

**Modified file: `apps/minds/imbue/minds/desktop_client/agent_creator.py`**
- In `_build_mngr_create_command()` (or wherever `--label workspace=<name>` is added), also add `--label user_created=true`.

## Implementation Phases

### Phase 1: Backend infrastructure (unified WebSocket + agent manager)

Build the workspace server backend without any frontend changes. The existing frontend continues to work via the existing `GET /api/agents` endpoint (keep it temporarily during this phase).

- Create `AgentManager` with `mngr observe` subprocess management, agent state tracking, and the custom `--events-dir`.
- Create `WebSocketBroadcaster` with connection management and event broadcasting.
- Add `GET /api/ws` WebSocket endpoint to `server.py`.
- Add `GET /api/random-name` endpoint.
- Wire `AgentManager` lifecycle into the FastAPI lifespan.
- Test: verify WebSocket sends initial snapshot, verify `mngr observe` events propagate to WebSocket clients.

### Phase 2: Application watching

Add application file watching to the agent manager.

- Implement `_watch_applications()` using watchdog to monitor `runtime/applications.toml` for each discovered agent.
- Parse TOML format: `[[applications]]` entries with `name` and `url` fields.
- Broadcast `applications_updated` events on changes.
- Handle agents being discovered/destroyed (start/stop watchers).
- Handle missing `runtime/applications.toml` gracefully (no applications for that agent).
- Test: verify applications appear in WebSocket stream, verify dynamic updates when `applications.toml` changes.

### Phase 3: Agent creation backend

Add REST endpoints and background creation for both agent types.

- Add `POST /api/agents/create-worktree` and `POST /api/agents/create-chat` endpoints.
- Implement background `mngr create` execution with log capture (similar pattern to the forwarding server's `AgentCreator` but simpler: no repo cloning, no Cloudflare setup).
- Pre-generate agent ID via `AgentId()`, pass to `mngr create --id`.
- Broadcast `proto_agent_created` and `proto_agent_completed` events.
- Add `GET /api/proto-agents/{id}/logs` WebSocket endpoint for log streaming.
- Test: verify creation triggers correct `mngr create` command, verify logs stream correctly, verify proto-agent lifecycle events.

### Phase 4: Template changes

Add new templates and labels.

- Create `.external_worktrees/forever-claude-template` worktree from `~/project/forever-claude-template`.
- Add "chat" and "worktree" create templates to `.mngr/settings.toml`.
- Update `agent_creator.py` in the minds desktop client to pass `--label user_created=true`.
- Test: verify `mngr create --template chat` produces a minimal agent, verify `mngr create --template worktree` produces an agent with no services.

### Phase 5: Frontend -- unified WebSocket and agent list

Replace the frontend's agent fetching with the WebSocket-based `AgentManager`.

- Create `AgentManager.ts` with WebSocket connection, state management, and reconnection.
- Update `ConversationSelector.ts` to read from `AgentManager` instead of fetching via REST.
- Filter sidebar to agents without `chat_parent_id`.
- Remove `GET /api/agents` endpoint from the backend (the temporary keep from Phase 1).
- Remove `fetchAgents()` from `Conversation.ts`.
- Test: verify sidebar updates in real-time when agents are created/destroyed.

### Phase 6: Frontend -- "+" tab menu overhaul

Replace the hard-coded dropdown with dynamic entries.

- Rewrite `createAddTabButton()` in `DockviewWorkspace.ts`:
  - Chat entries from `AgentManager.getChatAgentsForParent()`.
  - "New Chat" entry.
  - Application entries from `AgentManager.getApplicationsForAgent()`.
  - "Custom URL" entry.
  - Visual dividers between sections.
- Remove hard-coded "Terminal" entry.
- Update `addChatPanel()` to accept `chatAgentId`, use `chat-{chatAgentId}` panel IDs.
- Make all chat panels closable (remove `createUnclosableTab`).
- Update `focusOrCreateChatPanel` to `focusOrCreateChatPanelForAgent`.
- Update `ChatPanel.ts` to accept and use `chatAgentId`.
- Update layout persistence for variable chat panels.
- Test: verify menu shows correct entries, verify chat tab open/close/reopen, verify application iframe URLs are correct.

### Phase 7: Frontend -- agent creation modals and proto-agents

Complete the creation UX.

- Create `CreateAgentModal.ts` component.
- Create `ProtoAgentLogView.ts` component.
- Wire sidebar "+" button to open modal in worktree mode.
- Wire "New Chat" menu entry to open modal in chat mode.
- Add proto-agent rendering to `ConversationSelector.ts` (sidebar) and `DockviewWorkspace.ts` (tabs).
- Add proto-agent panel type to dockview.
- Handle creation completion: close log WebSocket, navigate to chat view.
- Test: verify end-to-end creation flow for both worktree and chat agents.

## Testing Strategy

### Unit tests

- `AgentManager`: mock `mngr observe` subprocess output, verify agent state tracking, verify application file watching triggers correct events, verify `mngr create` command construction (correct flags, labels, working directory).
- `WebSocketBroadcaster`: verify initial snapshot on connect, verify broadcast to multiple clients, verify disconnect cleanup.
- Proto-agent lifecycle: verify log queue management, verify completion events.
- Application TOML parsing: verify correct parsing of `[[applications]]` entries, verify handling of missing/empty files.

### Integration tests

- Start workspace server with a real `mngr observe` subprocess. Create an agent via `POST /api/agents/create-chat`. Verify proto-agent events appear on the unified WebSocket. Verify creation logs stream via the proto-agent WebSocket. Verify the agent appears in `agents_updated` after creation completes.
- Start workspace server, write to `runtime/applications.toml`, verify `applications_updated` event appears on the WebSocket.
- Verify `GET /api/random-name` returns a valid name.
- Verify `--events-dir` isolation: run workspace server alongside a separate `mngr observe` process and confirm no conflicts.

### Manual verification (via tmux for interactive components)

- Open the workspace in a browser. Verify the sidebar shows the main agent.
- Click "+" in the sidebar. Verify the modal appears with a random name. Confirm creation. Verify the proto-agent appears in the sidebar with "creating" state. Click it. Verify creation logs stream in real-time. Verify redirect to chat on completion.
- Select an agent in the sidebar. Click "+" in the tab header. Verify the menu shows "Chat (\<agent-name\>)", "New Chat", applications, and "Custom URL" in that order with dividers.
- Click "New Chat". Verify the modal, proto-agent tab, log streaming, and transition to chat.
- Click an application entry. Verify it opens an iframe at the correct proxied URL.
- Close a chat tab. Click "+" and select the same chat entry. Verify it reopens.
- Open a chat tab for an agent. Click the same entry in "+". Verify it focuses the existing tab instead of creating a duplicate.
- Add a new application to `runtime/applications.toml` while the workspace is open. Verify it appears in the "+" menu without page reload.

### Template tests

- Verify `mngr create --template chat` creates an agent with no extra tmux windows (no bootstrap, no services).
- Verify `mngr create --template worktree` creates an agent with no extra tmux windows.
- Verify workspace agents created by the desktop client have both `workspace=<name>` and `user_created=true` labels.

## Open Questions

- **Worktree creation from a worktree agent**: When the selected sidebar agent is itself a worktree agent and the user creates another worktree, should the new branch be based on that worktree agent's current branch (as specified), or should it always branch from the main agent's branch? The current spec says "from the selected agent's HEAD" which seems correct for maximum flexibility.
- **Chat agent cleanup**: When a worktree agent is destroyed, should its chat agents (agents with `chat_parent_id` pointing to it) be automatically destroyed too? The spec doesn't currently address agent destruction.
- **WebSocket reconnection behavior**: When the unified WebSocket reconnects after a drop, the full snapshot is re-sent. If a proto-agent was in progress during the disconnect, the frontend needs to reconcile its proto-agent state with the snapshot. The exact reconciliation logic is left to implementation.
- **Application URL mapping**: The spec assumes the application `name` in `applications.toml` matches the `server_name` used by the forwarding server's proxy routing. This relies on the `app-watcher` service registering applications with the same name. If there's a mismatch, the iframe URL will be wrong. Need to verify this invariant holds.
- **Agent state from `mngr observe`**: The `DiscoveredAgent` model from `mngr observe` provides agent metadata (name, labels, work_dir) but may not include the agent's runtime state (RUNNING, WAITING, STOPPED). If state is not in discovery events, the workspace server may need a supplementary mechanism to detect state changes (e.g., periodic `mngr list` calls or watching the agent's state file).
