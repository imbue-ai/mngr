# Sub-Agent Conversation View

Move sub-agent conversations out of the main agent timeline into separate, linked pages.

## Overview

- **Problem**: Sub-agent messages are currently shown inline in the main conversation, cluttering it with hundreds of events from spawned agents that obscure the primary conversation flow.
- **Solution**: Filter sub-agent events out of the main view. Replace Agent tool call blocks with styled link cards that open the sub-agent conversation in a new tab. Each sub-agent page reuses the same message rendering (markdown, tool calls) but is read-only (no message input).
- **Recursive**: Sub-sub-agents work the same way -- each sub-agent page links out to its own sub-agents, using the same pattern all the way down.
- **Metadata**: Sub-agent pages show the description and agentType from the `.meta.json` file in the header.

## Expected Behavior

### Main conversation view

- Only events from the main session are shown (sub-agent events are filtered out)
- When the main session uses the `Agent` tool, instead of a collapsible tool call block, a styled card appears showing:
  - The sub-agent's description (from `.meta.json`)
  - The agent type as a badge (e.g. "general-purpose", "imbue-code-guardian:validate-diff")
  - A "View conversation" link that opens `/agents/{agentId}/subagents/{subagentSessionId}` in a new tab
- The card replaces both the tool_use and tool_result events for Agent tool calls

### Sub-agent page

- URL: `/agents/{agentId}/subagents/{subagentSessionId}`
- Header shows the sub-agent description as the title and agentType as a badge
- Message list renders the sub-agent's events using the same components (user messages, assistant messages with markdown, collapsible tool call blocks)
- No message input (read-only)
- If the sub-agent spawned its own sub-agents, those appear as the same styled link cards (recursive)
- SSE streaming works so the page shows live updates if the sub-agent is still running

### Backend

- `GET /api/agents/{agentId}/events` returns only main-session events (events where `session_id` matches the main session, not sub-agent sessions)
- `GET /api/agents/{agentId}/subagents/{subagentSessionId}/events` returns events from only that sub-agent's session file
- `GET /api/agents/{agentId}/subagents/{subagentSessionId}/stream` provides SSE for live sub-agent updates
- Each `TranscriptEvent` includes a `session_id` field identifying which session file it came from
- Agent tool_use events in the main session are enriched with `subagent_metadata` containing the matched sub-agent's `agentType`, `description`, and `session_id` (parsed by extracting `agentId:` from the tool_result content via regex, then looking up the `.meta.json` file)

## Changes

### Backend: session_parser.py

- Add `session_id` parameter to `parse_session_lines()` so each event gets tagged with its origin session
- For Agent tool_result events, extract `agentId: <id>` from the result content text via regex
- Store extracted sub-agent IDs on the corresponding tool_use events for later metadata enrichment

### Backend: session_watcher.py

- Pass session_id when calling `parse_session_lines()` for each session file
- Read `.meta.json` files alongside sub-agent session files and store metadata (agentType, description) keyed by sub-agent session ID
- When returning events, filter by session: `get_all_events()` and `get_backfill_events()` accept an optional `session_id` parameter to return only events from that session
- Enrich Agent tool_use events with `subagent_metadata` from the stored metadata

### Backend: server.py

- Modify `_get_events` to only return main-session events (pass the main session_id filter)
- Add new endpoint `GET /api/agents/{agentId}/subagents/{subagentSessionId}/events` that returns events filtered to that sub-agent session
- Add new endpoint `GET /api/agents/{agentId}/subagents/{subagentSessionId}/stream` for SSE streaming of sub-agent events

### Frontend: models/Response.ts

- Add `session_id` and optional `subagent_metadata` fields to the `TranscriptEvent` interface
- Add `SubagentMetadata` interface with `agent_type`, `description`, `session_id` fields

### Frontend: views/MessageList.ts

- When rendering events, detect Agent tool calls (tool_name === "Agent") and render them as sub-agent link cards instead of collapsible tool call blocks
- The link card uses the `subagent_metadata` from the event to display description, agent type badge, and builds the URL from the agent ID route param + sub-agent session ID

### Frontend: views/SubagentView.ts (new)

- New page component for `/agents/{agentId}/subagents/{subagentSessionId}`
- Reuses message rendering from MessageList (user messages, assistant messages, tool calls)
- Shows header with description title and agentType badge
- No MessageInput component (read-only)
- Connects to SSE stream at `/api/agents/{agentId}/subagents/{subagentSessionId}/stream`
- Fetches events from `/api/agents/{agentId}/subagents/{subagentSessionId}/events`

### Frontend: index.ts

- Add route: `/agents/:agentId/subagents/:subagentSessionId` mapping to SubagentView

### Frontend: style.css

- Add styles for the sub-agent link card (description text, agent type badge, "View conversation" link, visual treatment distinct from regular tool call blocks)
