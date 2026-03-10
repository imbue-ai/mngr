---
name: list-event-types
description: List all event sources and types you may receive, with descriptions of their fields and meaning.
---

# Event sources and types

All events follow a standard envelope format:

```json
{"timestamp": "...", "type": "...", "event_id": "...", "source": "<source>", ...additional fields}
```

## messages

User and agent messages from conversation threads.

Additional fields:
- `conversation_id` - which conversation thread this message belongs to
- `role` - "user" or "assistant"
- `content` - the text content of the message

When you receive a `messages` event with `role: "user"`, a reply has already been generated and sent by the talking agent. Review the auto-generated reply (it will appear as a subsequent `messages` event with `role: "assistant"` for the same `conversation_id`) and decide whether follow-up action is needed.

## mng/agents

State transitions for sub-agents you launched via `delegate-task`.

Additional fields:
- `agent_id` - the ID of the sub-agent
- `state` - the new state (e.g. "finished", "blocked", "crashed", "waiting")
- `data` - additional metadata about the transition (e.g. error message if crashed)

When you receive this event, check whether the task was completed successfully and whether the user should be notified.

## scheduled

Scheduled trigger events. These fire at times you configured (or that were configured for you).

Additional fields:
- `data` - a payload describing the trigger, including its name and any metadata

React to scheduled events according to the instructions in the trigger's data payload.

## stop

Signals that you (the thinking agent) are about to stop. This is your last chance to check for unprocessed work before going to sleep.

Additional fields:
- `data` - metadata about the stop (if any)

When you receive this event, do a final check of your task list. If there is unfinished work, decide whether to handle it now or let it wait until you are next woken up.

## monitor (future)

Metacognitive reminders injected by a local monitor agent. Not yet implemented.

## conversations

Conversation lifecycle events (created, model changed). These are primarily bookkeeping. You do not typically need to react to these directly -- the `messages` source is more actionable.

Additional fields:
- `conversation_id` - the conversation that was created or modified
- `model` - the model being used for this conversation

## claude_transcript (log source, not an event source)

Your inner monologue transcript, written by Claude Code background tasks to logs/claude_transcript/events.jsonl (raw format, not in the event stream). This is a record of your own thinking and actions. You do not typically need to access these directly -- they exist so the talking agent and context tools can surface your recent thoughts to conversations.
