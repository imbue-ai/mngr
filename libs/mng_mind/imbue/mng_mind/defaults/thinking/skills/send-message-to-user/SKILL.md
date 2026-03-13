---
name: send-message-to-user
description: Send a message to the user in a conversation thread. Use to reply in an existing conversation, start a new conversation, or proactively reach out.
---

# Sending messages to the user

You communicate with users through conversation threads. Each thread has a unique `conversation_id` and a human-readable name.

## Starting a new conversation

When you want to proactively reach out (e.g. to notify the user about completed work, ask a question, or share an update), create a new conversation by choosing an appropriate, descriptive name:

```bash
$MNG_AGENT_STATE_DIR/commands/chat.sh --new --name "<descriptive name>" --as-agent "Your message here"
```

Choose a name that clearly describes the topic or purpose of the conversation. For example:
- "Build failure in auth module" for a bug report
- "PR #42 ready for review" for a completed task
- "Question about database migration" for a question

If a conversation with the given name already exists, your message will be added to that thread. Otherwise a new conversation is created.

This command prints the conversation ID to stdout.

## Injecting a message into an existing conversation

When you want to follow up in a conversation you already know about (e.g. responding to a user message in the same thread), inject your message directly using the `llm` tool:

```bash
llm inject --cid <conversation_id> -m <model> "Your message here"
```

You can find the `conversation_id` from the event you are responding to (it is included in `messages` events), or use the `list-conversations` skill. The `model` should match the model used by that conversation (also visible in the event data or conversation list).

## Choosing which approach to use

- If you are responding to a user message, use `llm inject` with the same `conversation_id` from the event so your reply appears in the same thread.
- If you are proactively notifying the user about something new (completed work, a question, an update), use `chat.sh --new --name "<name>" --as-agent` with a descriptive name.
- If you want to continue an existing topic, reuse the same name to add to the existing thread.
- If you are unsure, default to starting a new conversation with a descriptive name. Short, focused threads are easier for users to follow than long, multi-topic ones.

## Guidelines

- Keep messages concise and actionable.
- When notifying about completed work, include a summary and any relevant URLs (e.g. links to sub-agents or PRs).
- When asking questions, be specific about what you need to know.
- Reference the event or context that triggered the message when it helps the user understand why you are reaching out.
