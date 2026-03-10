---
name: list-conversations
description: List all active conversation threads for this changeling. Use when you need to see what conversations exist, check their models, or find a conversation ID.
---

# Listing Conversations

This skill lists all active conversation threads managed by this changeling.

## When to use

- You need to find the ID of a specific conversation to reference or resume
- You want to see an overview of all active conversations
- You need to check which model a conversation is using

## How to list conversations

Run the chat script with `--list`:

```bash
$MNG_HOST_DIR/commands/chat.sh --list
```

This reads the `changeling_conversations` table from the llm database and displays each conversation with:
- Conversation ID
- Creation timestamp
- Model being used

## Working with the results

- Use a conversation ID with `chat.sh --resume <id>` to continue an existing conversation
- Conversations are append-only; there is no explicit "close" operation
