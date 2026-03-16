# Event Lifecycle

This document describes how events flow through a mind, from creation to delivery to handling.

## Overview

Events are the primary communication mechanism for the thinking agent.
The thinking agent does not poll for work -- instead, batches of events are delivered to it by sticking them in a file and telling the agent to handle all events in that file via `mng message`, and it reacts to each event within.

## Event flow

```
Event source (DB, mng observe, scheduler, etc.)
    |
    v
Event log file ($MNG_AGENT_STATE_DIR/events/<source>/events.jsonl)
    |
    v
Event watcher (mng mindevents) -- reads via `mng events --follow`
    |
    v
Batching, rate limiting, chat pairing, aggregation
    |
    v
Batch file ($MNG_AGENT_STATE_DIR/mind/event_batches/<uuid>.jsonl)
    |
    v
Delivery via `mng message <agent-id> "Please process all events in <file>"`
    |
    v
Thinking agent reads the file, processes events, marks them handled
```

## Event sources

Each event source writes append-only JSONL to `$MNG_AGENT_STATE_DIR/events/<source>/events.jsonl`. Every event line has a standard envelope:

```json
{"timestamp": "...", "type": "...", "event_id": "...", "source": "<source>", ...additional fields}
```

Built-in sources:
- **messages** -- conversation messages, synced from the `llm` database by the conversation watcher
- **mng/agent_states** -- agent state transitions, written by the observer (`mng observe`)
- **scheduled** -- time-based triggers
- **stop** -- shutdown detection (last chance to handle pending work)
- **delivery_failures** -- event delivery failure notifications from the event watcher
- **conversations** -- conversation lifecycle events (creation, model changes)

## Supporting services

Two background processes (each running in their own `mng` agent tmux windows) handle event creation and delivery:

### Conversation watcher (`mng llmconversations`)

Polls the `llm` SQLite database for new messages in tracked conversations (those registered in the `mind_conversations` table). New messages are synced to `events/messages/events.jsonl` as `MessageEvent` records with `conversation_id`, `role`, and `content`.

User messages are always paired with assistant responses -- the watcher waits briefly for the assistant reply so the thinking agent sees both together.

### Event watcher (`mng mindevents`)

Streams all events via `mng events <agent-id> --follow` and delivers batches to the thinking agent. This happens via `event_watcher.py`, which handles a number of different concerns:

1. **Buffering**: Events are read from the subprocess into a shared buffer
2. **Catch-up filtering**: On restart, already-delivered events are skipped using persisted delivery state
3. **Chat pairing**: User messages from the `messages` source are held until the corresponding assistant response arrives (with a timeout)
4. **Aggregation**: If a source has too many events in one batch, or any single event is too large, all events from that source are written to a separate file and replaced with a single aggregate event pointing to that file
5. **Source filtering**: Events from excluded or dynamically ignored sources are dropped
6. **Rate limiting**: A token bucket controls delivery rate (configurable burst size and sustained rate)
7. **Delivery**: Events are written to `$MNG_AGENT_STATE_DIR/mind/event_batches/<uuid>.jsonl` and the file path is sent to the agent via `mng message`
8. **Retry**: On delivery failure, events are put back in the buffer with exponential backoff. After repeated failures, the user is notified via both a `delivery_failures` event and a chat message

## Handling and acknowledgement

When the thinking agent finishes processing a group of events, it calls `handle_event.sh` with the event IDs. This writes acknowledgement records to `events/handled_events/events.jsonl`.

Minds can use stop hooks (like `on_stop_prevent_unhandled_events.sh` for the `ClaudeMindAgent`) to compare all event IDs from batch files against handled event IDs. Then if any events are unhandled, the agent can be prevented from stopping. This ensures the thinking agent processes all events before going idle.  See `ClaudeMindAgent` for an example of this.

## Configuration

Event delivery behavior is configured via the `[watchers]` section in `minds.toml`. Key settings:

- `event_poll_interval_seconds` -- how often the event watcher polls (default: 3)
- `event_burst_size` -- initial burst allowance before rate limiting (default: 5)
- `max_event_messages_per_minute` -- sustained delivery rate (default: 10)
- `max_delivery_retries` -- consecutive failures before notifying the user (default: 3)
- `max_event_length` -- character limit before triggering aggregation (default: 50,000)
- `max_same_source_events_per_batch` -- event count limit per source before aggregation (default: 20)
- `event_exclude_sources` -- sources to never deliver (set during provisioning)

## Dynamic source filtering

The thinking agent can dynamically ignore event sources by writing source names (one per line) to `thinking/ignored_sources.txt`. The event watcher checks this file periodically and drops events from listed sources. This is used by the default `handle-events` skill to suppress unknown/unwanted sources.
