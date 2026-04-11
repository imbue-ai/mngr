# Claude Web Chat

Migrate ~/project/llm-webchat into apps/claude_web_chat, replacing the LLM backend with live views of mngr-managed Claude agent sessions.

## Overview

- **Replace LLM with mngr agents**: Instead of wrapping the `llm` CLI tool, the web chat reads Claude session JSONL files directly and sends messages via the mngr Python API. No dependency on `llm` at all.
- **Agent-based sidebar**: The left panel lists mngr-managed agents (via `list_agents()`), fetched once on page load. Each agent is one "chat." Subagent sessions are nested under their parent.
- **Live session file watching**: The backend watches raw Claude session JSONL files using watchdog (inotify) with mtime-based polling as a fallback safety net, reusing the pattern from `watcher_common.py` in `mngr_recursive`. Events are converted to common transcript format in pure Python within the app.
- **Tail-first loading**: When selecting an agent, the last 50 events load immediately, then earlier history backfills in the background. New events stream to the frontend via SSE (one stream per agent, same pattern as llm-webchat).
- **Preserve frontend stack and plugin system**: Keep Mithril.js + Tailwind CSS + Vite and retain the plugin/slot system for future extensibility.

## Expected Behavior

### Sidebar

- On page load, the sidebar lists all agents returned by `mngr list` (the Python API equivalent)
- Each entry shows the mngr agent name
- Subagent sessions appear nested under their parent agent
- The agent list does not auto-refresh; the user refreshes the page to see new agents
- Clicking an agent opens its conversation in the main panel

### Conversation view

- Selecting an agent immediately shows the last ~50 common transcript events (user messages, assistant messages, tool results)
- Earlier history loads in the background and prepends seamlessly
- New events from the session file appear in real-time via SSE as the agent works
- Assistant messages render markdown with DOMPurify sanitization
- Tool calls render as collapsible blocks: tool name as the header, input and output hidden by default, expandable on click
- Tool results appear associated with their corresponding tool call

### Sending messages

- A text input at the bottom allows sending messages to the agent
- Sending calls `send_message_to_agents()` from the mngr Python API with the agent's name/ID
- Fire-and-forget: no spinner or "sending" state. The message appears in the conversation when the agent's session file records it
- All agents are mngr-managed, so messaging is always available

### Session file discovery

- The backend reads `claude_session_id_history` from the agent's state directory to find all session IDs
- For each session ID, it searches `$CLAUDE_CONFIG_DIR/projects/` (defaulting to `~/.claude/projects/`) for matching `<session_id>.jsonl` files
- If a session file doesn't exist yet, the backend waits briefly, then logs a debug message and picks it up on the next poll cycle
- Multiple session IDs per agent are aggregated into a single conversation (same logic as common transcript: cross-session-boundary consolidation, deduplication via deterministic event IDs)

### Session file watching

- Uses watchdog (inotify on Linux, FSEvents on macOS) for low-latency change detection
- mtime-based polling runs as a safety net alongside watchdog, with backoff when no changes are detected
- Reuses the hybrid watchdog+mtime pattern from `watcher_common.py`: `ChangeHandler` for filesystem events, `mtime_poll_files()` for the polling fallback, `setup_watchdog_for_files()` for observer setup
- On each detected change, new lines from the session JSONL are read, converted to common transcript events, and pushed to connected SSE clients

### Server

- Defaults to 127.0.0.1:8000, no auto-open browser
- Standalone app with its own CLI entry point (not a mngr plugin)

## Changes

### New: `apps/claude_web_chat/`

- New standalone app under `imbue/claude_web_chat/` namespace, structured like `apps/minds/`
- `pyproject.toml` with dependency on `imbue-mngr` (not `imbue-mngr-claude`) and `watchdog`
- CLI entry point for starting the server (FastAPI + Uvicorn, same as llm-webchat)

### New: Session JSONL parser module

- Pure Python module that reads raw Claude session JSONL and converts to common transcript events (user_message, assistant_message, tool_result)
- Handles both string and array content formats in Claude messages
- Tracks tool_use_id to tool_name mappings across messages
- Truncates tool input previews (200 chars) and tool output (2000 chars)
- Deterministic event IDs derived from source UUIDs for deduplication
- Filters out noise event types (progress, file-history-snapshot, system, etc.)

### New: Session file watcher

- Manages per-agent file watchers using watchdog + mtime polling (pattern from `watcher_common.py`)
- Reads `claude_session_id_history` to discover session files, re-checks periodically for new sessions
- Maintains per-session byte/line offsets for incremental reads
- Feeds new events into per-agent SSE event queues

### New: Agent discovery endpoint

- `GET /api/agents` -- calls `list_agents()` from the mngr Python API, returns agent names, IDs, states, and any parent/child relationships for subagent nesting

### New: Agent conversation endpoints

- `GET /api/agents/{agent_id}/events` -- returns the last N common transcript events for tail-first loading
- `GET /api/agents/{agent_id}/events?before={event_id}` -- returns events before a given event for backfill
- `GET /api/agents/{agent_id}/stream` -- SSE stream of new events from the session file watcher
- `POST /api/agents/{agent_id}/message` -- sends a message via `send_message_to_agents()`, returns immediately

### Modified: Frontend sidebar

- Replace conversation list (fetched from LLM SQLite) with agent list (fetched from `/api/agents`)
- Show mngr agent name instead of conversation name/model
- Add nesting for subagent sessions under their parent

### Modified: Frontend message list

- Replace response-based rendering (LLM ResponseItems) with event-based rendering (common transcript events)
- Add collapsible tool call blocks (tool name header, expandable input/output)
- Implement tail-first loading: fetch last 50 events on select, then backfill via pagination

### Modified: Frontend streaming

- Replace subprocess-based SSE (llm stdout/stderr capture) with file-watcher-based SSE (session JSONL changes)
- SSE events carry common transcript event payloads instead of message deltas
- No replay buffer needed -- events are persisted in session files and served via the events endpoint

### Modified: Frontend message input

- Remove model selector (agents already have their model configured)
- Remove tool selector (agents manage their own tools)
- Send message via `POST /api/agents/{agent_id}/message` instead of spawning a subprocess
- Fire-and-forget: no busy indicator, response arrives via SSE stream

### Removed: LLM integration

- Remove all `llm` dependencies, SQLite database access, model/tool listing, subprocess spawning
- Remove `database.py`, `models.py` (LLM-specific data models)
- Remove conversation ID routing (replaced by agent ID routing)

### Removed: LLM-specific configuration

- Remove `LLM_WEBCHAT_CONVERSATION_IDS`, `LLM_WEBCHAT_TOOL_CHAIN_LIMIT` env vars
- Keep `host` and `port` configuration (same defaults)
- Keep `LLM_WEBCHAT_JAVASCRIPT_PLUGINS` and `LLM_WEBCHAT_STATIC_PATHS` for the plugin system (possibly renamed)
