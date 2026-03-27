# Conversation System

This document describes how conversations work within a mind.

## Overview

Conversations are the primary interface between users and minds. Each conversation is a thread of messages with a unique ID and a human-readable name. 
Users interact via a web-based chat interface; the mind's agents read and write messages programmatically.

## Architecture

```
User (browser)
    |
    v
llm live-chat (via ttyd web terminal) or web_browser.py
    |
    v
llm SQLite database ($LLM_USER_PATH/logs.db)
    |
    v
Conversation watcher (syncs from that sqlite database to events/messages/events.jsonl)
    |
    v
Event watcher (delivers all events, including message events, to thinking agent via mngr message)
    |
    v
Thinking agent (processes, delegates, replies via chat.sh, our wrapper around calls to llm)
    |
    v
llm SQLite database (message appears in DB)
    |
    v
User sees reply in llm live-chat (because it is sent SIGUSR1 via the llm-live-chat plugin) or it is sent via web_browser.py to the browser interface
```

## Storage

All conversation data lives in the `llm` tool's SQLite database at `$LLM_USER_PATH/logs.db`. This is the single source of truth for all chat data.

Two tables are relevant:
- **`conversations`** (llm-native) -- stores conversation ID and model
- **`mind_conversations`** (created during provisioning) -- stores mind-specific metadata: `conversation_id`, `tags` (JSON), `created_at`

The `tags` field is a JSON object used for categorization. Examples:
- `{"name": "Daily - 2026-03-15"}` -- a daily conversation
- `{"internal": "system_notifications"}` -- system notifications thread
- `{"internal": "slack_notifications"}` -- Slack integration notifications

Only conversations registered in `mind_conversations` are tracked by the conversation watcher.

## Special conversations

Several conversations are created automatically during provisioning:

- **Daily conversation** -- created each day with the name "Daily - YYYY-MM-DD". This is the default thread for proactive messages, daily summaries, and general discussion.
- **System notifications** -- tagged `{"internal": "system_notifications"}`. Used by the event watcher to notify the user about delivery failures and system issues. Not typically used by the thinking agent directly.

## Reading messages (as an agent)

The thinking agent receives messages as events from the `messages` source. Each event contains:
- `conversation_id` -- which thread the message belongs to
- `role` -- "user" or "assistant"
- `content` -- the message text

User messages always arrive paired with the auto-generated assistant response from the talking agent. The thinking agent reviews the response and decides whether follow-up is needed.

## Sending messages (as an agent)

### Replying in an existing conversation

Use `chat.sh` to add a message to a known conversation:

```bash
$MNGR_AGENT_STATE_DIR/commands/chat.sh --reply <conversation-id> "Your message here"
```

### Starting a new conversation

Use the chat script to create or resume a named conversation:

```bash
$MNGR_AGENT_STATE_DIR/commands/chat.sh --new --name "<descriptive name>" --as-agent "Your message here"
```

If a conversation with the given name already exists, the message is added to that thread. Otherwise, a new conversation is created. The command prints the conversation ID to stdout.

### Listing conversations

```bash
$MNGR_AGENT_STATE_DIR/commands/chat.sh --list
```

Shows all tracked conversations with their IDs, names, creation timestamps, and models.

## Message flow detail

1. **User sends message**: Types in `llm live-chat` web terminal. The message and the model's response are written to the `llm` database.
2. **Conversation watcher detects it**: Polls the database, finds new rows in `responses` table for tracked conversations, writes `MessageEvent` records to `events/messages/events.jsonl`.
3. **Event watcher delivers**: Picks up the new events, applies chat pairing (holds user messages until the assistant response arrives), batches them, and delivers to the thinking agent via `mngr message`.
4. **Thinking agent processes**: Reads the event batch file, sees the user message and auto-generated reply, decides on next steps (delegate work, send follow-up, etc.).
5. **Agent replies**: Uses `llm inject` or `chat.sh` to write a response back to the database.
6. **User sees reply**: The response appears in the `llm live-chat` interface.

## Conversation lifecycle

Conversations are append-only -- there is no explicit close operation. The daily conversation provides a natural rhythm for ongoing interaction, while topic-specific threads keep discussions focused.

Agents can suggest splitting a conversation into a new thread using `<SUGGEST_NEW_THREAD>` tags, or link to other conversations using `<THREAD_LINK>` tags. These are rendered specially in the web interface.

As future work, we will need to add "compaction" in order to consolidate conversations that grow very long (from the perspective of the talking agent, so that the conversation does not exceed the size of the context window).
