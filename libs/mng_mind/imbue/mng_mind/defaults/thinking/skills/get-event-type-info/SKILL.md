---
name: get-event-type-info
description: Get detailed information about a specific event type, including how to inspect the raw event log files.
---

# Getting detailed event type information

For a summary of all event types and their fields, use the `list-event-types` skill.

To inspect raw events for a specific source, read the corresponding log file:

```bash
cat "$MNG_AGENT_STATE_DIR/events/<source>/events.jsonl"
```

Where `<source>` is one of: `messages`, `mng/agents`, `scheduled`, `stop`, `conversations`, `monitor`.

## Useful commands for inspecting events

View the most recent events from a source:

```bash
tail -20 "$MNG_AGENT_STATE_DIR/events/messages/events.jsonl"
```

Count events per source:

```bash
wc -l "$MNG_AGENT_STATE_DIR/events/"*/events.jsonl
```

Find events for a specific conversation:

```bash
grep '"conversation_id":"<cid>"' "$MNG_AGENT_STATE_DIR/events/messages/events.jsonl"
```

Find events for a specific sub-agent:

```bash
grep '"agent_id":"<agent_id>"' "$MNG_AGENT_STATE_DIR/events/mng/agents/events.jsonl"
```
