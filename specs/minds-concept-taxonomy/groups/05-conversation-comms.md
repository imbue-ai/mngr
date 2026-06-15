# Group 5: conversation & communication

---

## chats / chat agents

### 1. Canonical Definition

A "chat agent" is a mngr agent created with `--template chat`. The template name is the sole code-level discriminator — there is no `ChatAgent` Python class. The canonical creation command is:

```
mngr create <name> --template chat
```

Code references:
- FCT `agent_manager.py:95-118` — `_build_chat_create_command()` hard-codes `--template chat` and `--transfer none`
- FCT `agent_manager.py:466-496` — `AgentManager.create_chat_agent()` calls the above
- FCT `server.py:698-705` — `POST /api/agents/create-chat` endpoint calls `create_chat_agent()`
- FCT `models.py:86-89` — `CreateChatRequest` (name field only)
- FCT `welcome_resend.py:9,139` — bootstrap creates exactly one chat agent per mind: `mngr create <host_name> --template chat --message /welcome`

### 2. All Usages

- Bootstrap mind creation: one initial chat agent named after the host, receives `/welcome`
- "New Chat" button in the UI: `POST /api/agents/create-chat` → `create_chat_agent()` (FCT `server.py:1086`)
- Layout refs use `chat:<agent_name>` and `chat-terminal:<agent_name>` (FCT `layout_ops.py:444,463`)
- Activity state in `activity_state.py:31` calls these "chat agent[s]" in its docstring

### 3. Competing / Multiple Definitions

There is no code-level enum or flag that marks an agent as a "chat" type at runtime. The label `user_created` appears in `AgentStateItem.labels` (FCT `models.py:57`) but does not indicate template type. The distinction from other agent types (worktree agents, background/worker agents) is purely by creation-time template.

The label `chat_parent_id` appears in `AgentStateItem.labels` (FCT `models.py:57`) — this links child agents to their parent chat agent but is not further defined in the read files.

### 4. Terminology Variants

- "chat agent" — used in code (docstrings, function names: `create_chat_agent`)
- "chat panel" — the UI panel showing the chat (`ChatPanel.ts`)
- Layout panel type `"chat"` — used in `layout_ops.py:240` as a string literal, not Python enum
- FCT CLAUDE.md uses "background agents" for mngr-managed agents generally; "chat agent" is a specific subtype with the `chat` template
- The mngr skills CLAUDE.md (in FCT) uses "sub-agent" in a Claude Code harness sense, and warns not to conflate with "background agents" (memory note: `feedback_minds_terminology_background_agents.md`)

### 5. Ambiguities / Inconsistencies

- No runtime type field distinguishes a chat agent from a worktree agent once created — only labels and creation_type in the proto-agent payload (`broadcast_proto_agent_created` passes `creation_type="chat"` in FCT `agent_manager.py:486`)
- The FCT `Conversation.ts` (frontend model) uses `Conversation` as a compatibility shim over `AgentState` — "conversation" and "agent" are conflated at the TS layer

### 6. DOC/CODE DIVERGENCES

None detected in the files read.

### 7. Recommended Canonical Term + Definition

**Canonical term: "chat agent"**

A mngr agent created with `--template chat` that supports interactive conversation with the user through the system_interface chat panel. Distinguished from worktree agents (code-editing) and background/worker agents by its template and its chat-panel visibility. The defining creation parameter is `--template chat`; no runtime enum exists.

---

## conversations / transcripts

### 1. Canonical Definition

A "transcript" (the code-dominant term) is the ordered sequence of parsed events derived from one or more Claude session JSONL files (`<session_id>.jsonl`) for a given mngr agent. The parser is:

- FCT `session_parser.py:162-216` — `parse_session_lines()`: parses raw Claude JSONL into `assistant_message`, `user_message`, and `tool_result` event dicts.

A single agent may have multiple session files (resumed sessions), concatenated chronologically. The watcher:

- FCT `session_watcher.py:221-283` — `AgentSessionWatcher`: watches all session files for an agent; manages a two-tier cache (locator index + bounded body LRU); emits parsed events via `on_events` callback.

Session files are discovered via `$AGENT_STATE_DIR/claude_session_id_history` (FCT `session_watcher.py:847`).

### 2. All Usages

**Python backend (FCT system_interface):**
- `session_parser.parse_session_lines()` — core parser; called from `_ensure_cache_current()` and `_reparse_line_locked()`
- `AgentSessionWatcher` — the live watcher; exposes `get_tail_events()`, `get_backfill_events()`, `get_forward_events()`, `get_events_at_offset()`, `get_all_events()`
- `SessionFileState` (FCT `session_watcher.py:196-218`) — per-file state: byte offset, locator list, emitted count
- `EventLocator` (FCT `session_watcher.py:160-193`) — compact (event_id, timestamp, byte_offset, byte_len) pointer; avoids holding all event bodies in memory

**Frontend (FCT):**
- `Response.ts` — `TranscriptEvent` union type: `UserMessageEvent | AssistantMessageEvent | ToolResultEvent`; `TranscriptStore` class manages the loaded window; `fetchEvents()`, `fetchBackfillEvents()`, `fetchForwardEvents()`, `fetchWindowAtOffset()`
- `Conversation.ts` — backward-compat shim; `getConversations()` delegates to `AgentManager`; `Conversation` interface exists only for plugin/hook compat

**Minds desktop client:**
- `welcome_resend.py:165-191` — `_default_read_assistant_transcript()` uses `AgentSessionWatcher.get_all_events()` to check for `/welcome` delivery

**mngr:**
- `cli/transcript.py` — `mngr transcript` command (file found in mngr_message_files.txt)

### 3. Competing / Multiple Definitions

- In FCT frontend `Conversation.ts:15-20`, `Conversation` is an interface with `id, name, model, latest_response_datetime_utc` — a legacy shape that maps agent→conversation. It does NOT represent a transcript; it is a backward-compat wrapper.
- `AgentEventQueues` (FCT `event_queues.py:9`) was adapted from "llm-webchat's ConversationEventQueues" — the old term "conversation" is now "agent" in the live code but the comment reveals the lineage.

### 4. Terminology Variants

- **transcript** — used in code for the parsed event sequence (session_parser, session_watcher, `parse_session_lines`, `TranscriptEvent`)
- **session** — used for a single Claude JSONL file (a "session file", `session_id`, `SessionFileState`); a transcript spans multiple sessions
- **events** — the parsed output objects (`transcript events`); also the REST path `/api/agents/{id}/events`
- **conversation** — used only as a legacy/compat term in FCT frontend (`Conversation.ts`, `ConversationEventQueues` comment); deprecated in favor of "agent"
- **common transcript events** — the internal label for the parsed event format (FCT `session_parser.py:1`: "common transcript events")

### 5. Ambiguities / Inconsistencies

- The REST endpoint is `/api/agents/{id}/events` (FCT `server.py`) — it returns transcript events, but the path word is `events`, not `transcript` or `conversations`.
- `ConversationNotFoundError` in FCT `Response.ts:635` is a compat class; the real 404 path is `notFoundAgentIds` set.
- "session" overloads: both the auth concept (user login session, `AccountSession` in minds `session_store.py`) and the Claude JSONL session file are called "session." These are entirely distinct.

### 6. DOC/CODE DIVERGENCES

- FCT `event_queues.py:12` comment says "Adapted from llm-webchat's ConversationEventQueues" — the old class was named after conversations; the new one is named after agents. Doc comment is historical, not an inaccuracy, but worth noting.

### 7. Recommended Canonical Term + Definition

**Canonical term: "transcript"** (for the parsed event sequence), **"session"** (for a single Claude JSONL file).

- **Transcript**: the full ordered sequence of `user_message`, `assistant_message`, and `tool_result` events derived from one or more Claude session JSONL files for a given agent. Multiple session files (from `claude --resume`) are concatenated chronologically into a single transcript.
- **Session**: one `<session_id>.jsonl` file produced by Claude Code. A transcript is built from one or more sessions.
- Retire "conversation" from production code. It is a legacy term (originally from llm-webchat) that now appears only in compat shims.

---

## messages

### 1. Canonical Definition

"Message" is overloaded across two distinct concepts:

**A. Inbound user message to an agent (mngr message delivery)**

Delivery of text into a running agent's tmux stdin via `mngr message`. The canonical code path:

- `libs/mngr/imbue/mngr/api/message.py:42` — `send_message_to_agents()`: takes a pre-resolved set of `AgentMatch` objects (`agents_to_message`), groups them by host via `group_agents_by_host()`, and calls `agent.send_message(message_content)` per agent (concurrently). Host/agent discovery and filtering happen in the caller (the CLI obtains `agents_to_message` from `find_all_agents()`), not inside this function.
- `libs/mngr/imbue/mngr/cli/message.py:53` — `mngr message` CLI command; resolves agent addresses via `find_all_agents()` (`cli/message.py:143`) and passes the matches to `send_message_to_agents()`. The `MessageResult` model tracks successful/failed agents
- FCT `agent_discovery.py:send_message` — thin wrapper used by `welcome_resend.py`
- FCT `models.py:27-36` — `SendMessageRequest` / `SendMessageResponse` (REST body/response)
- FCT `server.py` — `POST /api/agents/{id}/message` endpoint

The mechanism: `agent.send_message(message_content)` is called on the `AgentInterface`; for tmux-based agents this sends keystrokes to the tmux pane. There is no message queue or persistent message store — messages are fire-and-forget stdin injections.

**B. Transcript events rendered in the chat UI**

The `user_message` and `assistant_message` events from the parsed transcript (see "conversations / transcripts"). These are stored in Claude session JSONL and surfaced via the `/events` endpoint.

- FCT `session_parser.py:334-350` — `user_message` event (the user's text to Claude)
- FCT `session_parser.py:219-310` — `assistant_message` event (Claude's response)
- FCT `Response.ts:61-87` — `UserMessageEvent`, `AssistantMessageEvent` interfaces
- FCT `models.py:27-29` — `SendMessageRequest.message` — the text sent via `POST /api/agents/{id}/message`

**C. Notification message (see notifications section below)**

`NotificationRequest.message` (minds `notification.py:70`) is a third distinct use: the body text of an OS-level notification.

### 2. All Usages

- `mngr message` CLI (`cli/message.py`) — external tool; sends text into an agent
- `mngr api/message.py:send_message_to_agents()` — programmatic send
- `POST /api/agents/{id}/message` (FCT `server.py`) — REST endpoint for frontend "send message" input box
- `Response.ts:sendMessage()` — frontend function calling the REST endpoint
- `MessageInput.ts` — frontend component; calls `sendMessage()`
- Claude session JSONL `"type": "user"` entries — the source-of-truth for user messages delivered to Claude; parsed into `user_message` events by `session_parser.py`

### 3. Competing / Multiple Definitions

Three distinct senses all use "message":
1. **mngr message** — a string injected into an agent's stdin
2. **transcript message** — a `user_message` or `assistant_message` parsed event from the JSONL
3. **notification message** — the body text of a desktop notification (`NotificationRequest.message`)

Additionally:
- `MngrMessageSender` in minds `latchkey/handlers/messaging.py:55` — a wrapper around `mngr message <agent-id> <text>`, shared by the predefined and file-sharing permission handlers to notify the waiting agent (via `mngr message`) once its permission request is resolved. This is the same "send a message to an agent's stdin" sense (A) above, not a distinct one.

### 4. Terminology Variants

- "message" — used for all three senses above
- "message_content" — the string being sent via `mngr message` (`MessageCliOptions.message_content`, `send_message_to_agents` parameter)
- "user_message" / "assistant_message" — transcript event type strings
- `SendMessageRequest.message` — the field name in the REST body

### 5. Ambiguities / Inconsistencies

- The FCT REST body for sending a message to an agent (`SendMessageRequest.message`) and the transcript's `user_message.content` both hold "the text the user sent," but they are different objects at different lifecycle stages: one is the inbound HTTP request, the other is the parsed JSONL event recorded after Claude receives it.
- `mngr message` sends to tmux stdin; the frontend's `POST /api/agents/{id}/message` also triggers a tmux stdin injection — they are the same underlying mechanism via `agent.send_message()`.

### 6. DOC/CODE DIVERGENCES

None detected.

### 7. Recommended Canonical Term + Definition

Disambiguate by context:
- **"send a message"** / **"mngr message"** — injecting text into a running agent's stdin. The `mngr message` command is the canonical external API.
- **"transcript event"** (specifically `user_message` or `assistant_message` event) — the parsed representation of a Claude session JSONL entry.
- **"notification"** — the OS-level desktop alert (separate concept, see notifications section).

Do NOT use "message" alone in documentation or APIs without qualifying which sense is meant.

---

## plans

### 1. Canonical Definition

A "plan" in the user-facing sense is the rendered timeline of tk step records for a single user turn, shown in the chat progress view. The code calls this a "progress view" or "progress block" — the word "plan" does not appear as a code term.

The data model for a plan (progress view) is built from two sources:
1. **Transcript events** — the parsed JSONL stream; `tk start`/`tk close` transitions appear as `tool_result` events whose output matches `Updated <id> -> in_progress|closed` (FCT `turn-grouping.ts:120`, regex `TK_UPDATED_RE`)
2. **Step enrichment** — a snapshot side-table keyed by ticket_id: title, summary, status, created_at (FCT `Response.ts:111-116`, `StepEnrichment` interface; delivered as `step_enrichment` field on `/events` responses and as SSE `step_enrichment` messages)

The rendered output is a `SectionView` (FCT `turn-grouping.ts:98-107`) containing:
- `user_event` — the boundary user message
- `items: TimelineItem[]` — ordered step nodes, ungrouped runs, interjections, chips
- `trailing_reply` — prose after the last tool activity (the user-facing reply)

Steps are parsed into `StepNode` objects (FCT `turn-grouping.ts:56-80`) with ticket_id, title, status (pending/active/done), summary, narration, events.

The UI component is `ProgressBlock` (FCT `views/ProgressBlock.ts:91`) — renders the timeline, status icons, expand/collapse.

### 2. All Usages

- FCT `turn-grouping.ts` — `buildSections()`: the single-pass transcript walk that produces `SectionView[]`; recognizes `tk create/start/close` calls, tracks the "current open step," groups events
- FCT `ProgressBlock.ts` — renders one section's timeline
- FCT `ChatPanel.ts:151-165` — instantiates `ProgressBlock` per section with steps
- FCT `tickets_parser.py` — parses `.tickets/<id>.md` files into `TicketState`; the source of enrichment titles/summaries
- FCT `tickets_watcher.py` (file not fully read) — watches `.tickets/` for changes; pushes enrichment snapshots
- FCT `Response.ts:StepEnrichment` / `applyEnrichmentSnapshot()` — client-side enrichment table

### 3. Competing / Multiple Definitions

- FCT CLAUDE.md uses "plan" in the task-management sense: "decompose it into a sequence of step records" and "the sequence of steps is the user-visible plan." This is a documentation concept, not a code class.
- The FCT CLAUDE.md also says "This component renders structure it is given; it does no grouping or ordering itself" (from ProgressBlock docstring) — confirming that "plan" is not a first-class code concept.
- mngr has a `ticket` system (the tk tracker); tickets that are `step: true` in frontmatter are the underlying records. See Group 4 for ticket/step details.

### 4. Terminology Variants

- **"progress view"** — the UI element (docstrings, FCT CLAUDE.md)
- **"progress block"** — the UI component (`ProgressBlock`)
- **"timeline"** — used in code for the list of `TimelineItem[]`
- **"plan"** — used only in FCT CLAUDE.md documentation ("the sequence of steps is the user-visible plan"); not a code term
- **"steps"** — the tk step records that populate the timeline

### 5. Ambiguities / Inconsistencies

- The word "plan" appears in the CLAUDE.md instructions to agents but not in the production Python/TypeScript code. The code term is "progress view," "timeline," or "sections."
- A "step" in the progress view sense (a `StepNode`) is rendered from a tk ticket with `step: true` frontmatter — but the `TicketState` model (FCT `tickets_parser.py:50`) uses `step: bool` field. The term "step" is shared between the enrichment layer (ticket files) and the UI layer (StepNode), with consistent semantics.

### 6. DOC/CODE DIVERGENCES

- FCT CLAUDE.md says "every step title and every closing summary is user-facing copy" — this is correct and grounded in `StepNode.title` / `StepNode.summary` rendered by `ProgressBlock`.
- No DOC/CODE divergence found; docs match implementation.

### 7. Recommended Canonical Term + Definition

**Canonical term: "progress view"** (for the UI), **"step"** (for each timeline node).

- **Progress view**: the rendered vertical timeline of tk steps for one user turn, shown in the chat panel between the user's message and the agent's wrap-up reply.
- **Step**: a single turn-bound progress record (`step: true` ticket) representing one logical task unit. Rendered as a `StepNode` in the timeline.
- Reserve "plan" for human-facing documentation if needed; do not introduce it as a code term.

---

## inbox

### 1. Canonical Definition

The "inbox" is the desktop client UI panel (modal/drawer) showing pending permission requests. The canonical code model is `RequestInbox` (minds `request_events.py:213`):

```python
class RequestInbox(FrozenModel):
    """Aggregates request and response events to compute the pending inbox.
    Maintains two ordered lists: requests and responses. The pending inbox
    is every request, keyed only by ``event_id``, that has no corresponding
    response."""
    requests: list[RequestEvent]
    responses: list[RequestResponseEvent]
```

The inbox is an event-sourced aggregate: it replays `RequestEvent` and `RequestResponseEvent` objects to compute which requests are still pending.

### 2. All Usages

**Python model:**
- `request_events.py:213-291` — `RequestInbox` class; methods: `add_request()`, `add_response()`, `get_pending_requests()`, `get_request_by_id()`, `is_request_resolved()`, `get_pending_count()`
- `app.py` (minds desktop client) — stores `RequestInbox` on `app.state`; updated by event stream consumers

**Template rendering:**
- `templates.py:474-518` — `render_inbox_page()`, `render_inbox_list_fragment()`, `render_inbox_unavailable_fragment()`
- Template function signatures show the inbox is rendered from `cards` (a sequence of dicts), `selected_id`, and `detail_html`

**Gateway consumer:**
- `latchkey/permission_requests_consumer.py:108` — `PermissionRequestsConsumer`: the background thread that streams permission requests from the latchkey gateway and injects them into the inbox

**Routes (in app.py):**
- `GET /inbox` — full inbox page
- `GET /inbox/list` — left-panel list fragment
- `GET /inbox/detail/<id>` — right-pane detail fragment

### 3. Competing / Multiple Definitions

The word "inbox" appears only in the context of permission requests. There is no "notification inbox" or "message inbox" — the inbox is exclusively for pending permission requests.

### 4. Terminology Variants

- **"inbox"** — used in templates, route paths, Python variable names
- **"pending requests"** — used in `get_pending_requests()`, docstrings
- **"cards"** — the individual items in the inbox left-list (template parameter name: `cards`)
- **"request"** — the generic term for an inbox item before it is approved/denied

### 5. Ambiguities / Inconsistencies

- The inbox model (`RequestInbox`) aggregates all request types (PERMISSIONS, LATCHKEY_PERMISSION, FILE_SHARING_PERMISSION) — there is no subclass per request type. The rendering is type-dispatched via `RequestEventHandler` subclasses.
- "card" is used only in template rendering context (parameter `cards`), while the Python model uses `RequestEvent`. The same concept is called "card" (template layer) and "request event" (model layer).

### 6. DOC/CODE DIVERGENCES

- `request_events.py:1` module docstring mentions `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` as the source for agent-written request events. However, since latchkey 2.9.0, the primary source is the streaming `GET /permission-requests?follow=true` gateway endpoint (via `PermissionRequestsConsumer`), not JSONL files written by agents. The JSONL path is still used for response events (written by the desktop client), but request events now flow primarily via the gateway stream. The docstring is partially out of date.

  **DOC/CODE DIVERGENCE**: `request_events.py:1-12` says "Agents write request events to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl`" but `permission_requests_consumer.py:6-9` states this is the pre-latchkey-2.9.0 path; requests now come from the gateway stream.

### 7. Recommended Canonical Term + Definition

**Canonical term: "inbox"**

The permission-request inbox: a modal panel in the desktop client listing all pending agent permission requests (latchkey permission, file sharing, general permissions). Modeled as an event-sourced aggregate (`RequestInbox`) of `RequestEvent` and `RequestResponseEvent` objects. Individual inbox items are "permission requests" (or "request cards" in template context).

---

## permission requests / approvals

### 1. Canonical Definition

A "permission request" is a structured event an agent emits to ask the user to authorize a resource access. The base class is `RequestEvent` (minds `request_events.py:62`):

```python
class RequestEvent(EventEnvelope):
    agent_id: str
    request_type: str  # "PERMISSIONS", "LATCHKEY_PERMISSION", "FILE_SHARING_PERMISSION"
    is_user_requested: bool  # if True, desktop client auto-navigates to the request page
```

Three concrete subtypes:
- `PermissionsRequestEvent` (`request_events.py:73`): `resource`, `description`
- `LatchkeyPredefinedPermissionRequestEvent` (`request_events.py:80`): `scope`, `permissions`, `rationale`
- `LatchkeyFileSharingPermissionRequestEvent` (`request_events.py:104`): `path`, `access`, `rationale`

The outcome is a `RequestResponseEvent` (`request_events.py:131`):

```python
class RequestResponseEvent(EventEnvelope):
    request_event_id: str  # event_id of the original request
    status: str  # "GRANTED" or "DENIED"
    agent_id: str
    scope: str | None
    request_type: str
```

The outcome enum is `RequestStatus` (`request_events.py:47`):
```python
class RequestStatus(UpperCaseStrEnum):
    GRANTED = auto()
    DENIED = auto()
```

Note: there is no `FAILED` status in the code. The enum has only `GRANTED` and `DENIED`. (The task description / `Minds_concepts.md` lists a "failed" outcome, but no such value exists anywhere in `request_events.py` — neither the enum nor the module docstring mentions it.)

### 2. All Usages

**Request creation:**
- `create_latchkey_predefined_permission_request_event()` (`request_events.py:148`)
- `create_latchkey_file_sharing_permission_request_event()` (`request_events.py:170`)
- `create_request_response_event()` (`request_events.py:192`)

**Request parsing:**
- `parse_request_event()` (`request_events.py:293`) — discriminates by `request_type` field
- `parse_response_event()` (`request_events.py:324`) — handles legacy `service_name` field stripping
- `load_response_events()` (`request_events.py:338`) — loads from `~/.minds/events/requests/events.jsonl`
- `append_response_event()` (`request_events.py:357`) — writes response to disk
- `write_request_event_to_file()` (`request_events.py:367`) — appends request to JSONL

**Gateway consumer:**
- `permission_requests_consumer.py:62` — `streamed_request_to_event()`: translates a gateway-streamed `StreamedPermissionRequest` into a `RequestEvent` subclass
- `PermissionRequestsConsumer` — background thread consuming the gateway stream

**Request handlers (type-dispatch):**
- `request_handler.py:27` — `RequestEventHandler` abstract base
- `latchkey/handlers/predefined.py` — handles `LATCHKEY_PERMISSION`
- `latchkey/handlers/file_sharing.py` — handles `FILE_SHARING_PERMISSION`

**Routes (in app.py):**
- `GET /inbox/detail/<id>` — renders detail fragment
- `POST /requests/<id>/grant` → `apply_grant_request()`
- `POST /requests/<id>/deny` → `apply_deny_request()`
- `POST /permission-requests/approve/<id>` (latchkey gateway path for file sharing)
- `DELETE /permission-requests/<id>` (latchkey gateway path for deny)

### 3. Competing / Multiple Definitions

- The `RequestType` enum (`request_events.py:39`) defines `PERMISSIONS`, `LATCHKEY_PERMISSION`, `FILE_SHARING_PERMISSION`. The first (`PERMISSIONS`) is a generic type; the latter two are latchkey-specific.
- The gateway (`GET /permission-requests?follow=true`) uses its own payload types: `PredefinedRequestPayload` (`gateway_client.py:92`) and `FileSharingRequestPayload` (`gateway_client.py:120`), imported into `permission_requests_consumer.py:38,41` and translated into the inbox's `RequestEvent` hierarchy at ingestion.

### 4. Terminology Variants

- **"permission request"** — the dominant term (module docstring, function names)
- **"request event"** — the code object (`RequestEvent`, `RequestResponseEvent`)
- **"request"** — used in route paths (`/requests/<id>/grant`)
- **"approval"** / **"grant"** — used interchangeably for the positive outcome (`apply_grant_request`, `GRANTED`)
- **"deny"** — the negative outcome (`apply_deny_request`, `DENIED`)
- **"card"** — how individual inbox items are called in template code
- **"latchkey permission request"** — the specific subtype for latchkey-managed scopes
- **"file-sharing permission request"** — the specific subtype for per-path filesystem access

### 5. Ambiguities / Inconsistencies

- The task description says "granted/denied/failed outcomes" but the `RequestStatus` enum has only `GRANTED` and `DENIED` — there is no `FAILED` value in code. `FAILED` is not a real code concept for request outcomes.
- `is_user_requested` (`RequestEvent.is_user_requested`) means "the desktop client should auto-navigate to this request." The name is ambiguous — it sounds like it means "was requested by the user" but it means "auto-open the detail page."
- `scope` on `RequestResponseEvent` is described as "Informational only" (docstring) — filtering uses `request_event_id`, not `scope`. This is noted explicitly in the code, but the field's presence could mislead.

### 6. DOC/CODE DIVERGENCES

**DOC/CODE DIVERGENCE**: The task description (Minds_concepts.md) lists "granted/denied/failed outcomes" but `RequestStatus` (`request_events.py:47`) has only `GRANTED` and `DENIED`. No `FAILED` enum value exists.

**DOC/CODE DIVERGENCE** (partial): `request_events.py:3-7` says agents write request events to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl`, but `permission_requests_consumer.py:6-9` clarifies this was pre-latchkey-2.9.0; the modern path is the gateway stream.

### 7. Recommended Canonical Term + Definition

**Canonical term: "permission request"**

A structured event emitted by an agent requesting user authorization for a resource access (latchkey scope, file path, or general permission). The request is represented as a `RequestEvent` (or subclass), stored in the inbox, and resolved by the user as `GRANTED` or `DENIED` (not `FAILED`) via the inbox UI. The `RequestResponseEvent` records the resolution outcome.

---

## notifications

### 1. Canonical Definition

A "notification" is an OS-level user alert dispatched by the desktop client when an agent calls the notification API. The canonical model is `NotificationRequest` (minds `notification.py:67`):

```python
class NotificationRequest(FrozenModel):
    message: str  # notification body text
    title: str | None  # optional title
    urgency: NotificationUrgency  # LOW, NORMAL, CRITICAL
    url: str | None  # URL to navigate to on click
```

The urgency enum (`notification.py:34`):
```python
class NotificationUrgency(UpperCaseStrEnum):
    LOW = auto()
    NORMAL = auto()
    CRITICAL = auto()
```

Dispatch is handled by `NotificationDispatcher` (`notification.py:268`), which routes to one of three channels:
- `DispatchChannel.ELECTRON` — stdout JSONL (`emit_event("notification", ...)`)
- `DispatchChannel.MACOS` — native macOS `osascript` notification
- `DispatchChannel.TKINTER` — tkinter toast window (bottom-right corner)

Priority: Electron > macOS native > tkinter.

The REST endpoint is `POST /api/v1/agents/{agent_id}/notifications` (`api_v1.py:181`).

### 2. All Usages

**Python:**
- `notification.py` — full implementation: `NotificationRequest`, `NotificationUrgency`, `NotificationDispatcher`, `DispatchChannel`
- `api_v1.py:122-165` — `_handle_notification()`: parses HTTP body, constructs `NotificationRequest`, calls `dispatcher.dispatch()`
- `api_v1.py:171-183` — router factory; registers `POST /agents/{agent_id}/notifications`

**Electron main process:**
- `emit_event("notification", data, OutputFormat.JSONL)` in `_dispatch_electron_notification()` (`notification.py:103`) — Electron parses this JSONL from stdout to show a native notification

**FCT (system_interface):**
- No notification system found in FCT — the notification mechanism is minds-desktop-client-only.

### 3. Competing / Multiple Definitions

- "notification" in the `NotificationRequest` model is an OS-level alert (toast/native popup).
- "notification" in `emit_event("notification", ...)` (`notification.py:103`) is the JSONL event type string for the Electron channel — an output event, not an OS alert at this layer.
- SSE events in the `AgentEventQueues` are sometimes called "events" and sometimes could colloquially be called "notifications" — but the code never calls them "notifications."

### 4. Terminology Variants

- **"notification"** — the OS-level alert (`NotificationRequest`)
- **"toast"** — used for tkinter popup (`_show_tkinter_toast`, `_run_tkinter_toast`)
- **"notification"** (JSONL event type) — the event emitted to Electron stdout (`emit_event("notification", ...)`)
- **"urgency"** — the severity level (`NotificationUrgency`)
- The Electron main process presumably has its own notification rendering code (not read) that consumes the JSONL `"notification"` event

### 5. Ambiguities / Inconsistencies

- The `NotificationRequest.url` field (`notification.py:76`) is defined and serialized to the Electron channel but is not used by the macOS (`osascript`) or tkinter (`_show_tkinter_toast`) dispatch paths — they ignore `url`. This means click-to-navigate only works in Electron.
- `NotificationRequest` is named "Request" though it is also the data model for what gets dispatched — it is both the input and the internal representation (no separate "Notification" vs "NotificationRequest" split).

### 6. DOC/CODE DIVERGENCES

None detected.

### 7. Recommended Canonical Term + Definition

**Canonical term: "notification"**

An OS-level user alert dispatched by the desktop client on behalf of an agent, with a message body, optional title, urgency level (LOW/NORMAL/CRITICAL), and optional click URL. Agents trigger notifications via `POST /api/v1/agents/{id}/notifications`. Delivery is routed to Electron (JSONL to stdout), macOS native (osascript), or tkinter toast depending on runtime context.

Distinguish from:
- **Permission request** — an inbox item requiring user approval
- **SSE event** — a server-sent event to the frontend (different system entirely)

---

## Cross-Cutting Inconsistencies

1. **"message" is triple-overloaded**: it means (a) text injected into agent stdin via `mngr message`, (b) a `user_message`/`assistant_message` transcript event, and (c) the body of a desktop notification (`NotificationRequest.message`). No disambiguation convention exists in the current codebase.

2. **"notification" vs "request" vs "event" overlap**: A gateway-streamed permission request arrives as a `StreamedPermissionRequest`, is converted to a `RequestEvent` (which is an `EventEnvelope`), and the desktop client sends a `RequestResponseEvent` to record the outcome. All of these are "events" in code but only some are "notifications" colloquially. The `emit_event("notification", ...)` call in `notification.py` adds a fourth sense ("notification" as an Electron JSONL event type).

3. **"card" vs "request" for inbox items**: The template layer calls inbox items "cards" (parameter name `cards`), the model layer calls them `RequestEvent` objects. No consistent term spans both layers.

4. **"conversation" vs "transcript" vs "session"**: Three terms for overlapping concepts. "Conversation" is a legacy term (kept only in FCT frontend compat shims), "transcript" is the parsed event sequence, "session" is one Claude JSONL file. Mixing these three creates confusion about granularity and scope.

5. **"plan" appears only in FCT CLAUDE.md documentation, not in code**: The user-visible progress timeline is called "progress view" / "progress block" / "sections" / "timeline" in code — never "plan." Docs call it a plan. This divergence will mislead contributors.

6. **"chat agent" has no runtime type marker**: Only the `creation_type: "chat"` field in `proto_agent_created` WebSocket events distinguishes a chat agent from other agents during creation; after creation, no persistent label captures the template type. The label `user_created` is a heuristic, not a template indicator.

7. **`RequestStatus` missing `FAILED`**: The task description (Minds_concepts.md) lists "failed" as an outcome, but only `GRANTED` and `DENIED` exist in `RequestStatus`. The term "failed" does not appear in the outcome enum.

8. **"inbox" is exclusively permission requests**: Despite the generic term, the inbox contains only permission requests (not messages, not notifications). The naming implies more generality than the current implementation supports — a potential source of confusion if new card types are added.

9. **Two "session" concepts collide**: Auth sessions (`AccountSession`, `MultiAccountSessionStore` in minds `session_store.py`) and Claude JSONL session files (`SessionFileState`, `session_id`) both use "session." These are completely distinct concepts sharing one word.
