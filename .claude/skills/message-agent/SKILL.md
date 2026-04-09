---
name: message-agent
argument-hint: <agent_name> <description of what to say>
description: Send a message to another mngr agent. Use when you need to communicate with a peer agent.
allowed-tools: Bash(echo *), Bash(uv run mngr message *), Bash(uv run mngr list *), Write(*)
---

The user's message contains a target agent name (the first word) and a description of what to communicate. Extract the agent name and treat everything after it as the intent/content of the message.

Note: the user may paste a git branch name like `mngr/some-agent` instead of the bare agent name. In that case, strip the `mngr/` prefix to get the actual agent name (e.g. `mngr/better-tabcomplete` -> `better-tabcomplete`).

## Agent Name Resolution

First, verify the target agent exists by running:

```
uv run mngr list --format '{name}'
```

If the extracted name doesn't match any agent exactly, check if the user's input was a description (e.g. "the agent working on X") rather than a name, and try to match against the listed agents and their git branches. If there's an unambiguous match, use it. Otherwise, use AskUserQuestion to ask the user which agent they meant, presenting the plausible candidates.

## Your Agent Name

Your own agent name is available in the `MNGR_AGENT_NAME` environment variable. Read it:

```bash
echo "$MNGR_AGENT_NAME"
```

You will use this to identify yourself in the message.

## Composing the Message

Based on the user's description, compose the full message. Every message you send MUST:

1. **Start with a sender tag**: `[from: <your agent name>]` (using the value of `$MNGR_AGENT_NAME`).
2. **Contain the actual content**: Write the message based on what the user described. Be clear and direct.
3. **End with a reply instruction**: Close with a line like: `To reply, use the /message-agent skill.`

Example message:

```
[from: refactor-auth]

Hey -- I just finished refactoring the auth middleware on my branch. You'll want to rebase before merging since I changed the SessionStore interface. The new method is `get_session_by_token()` instead of `lookup()`.

To reply, use the /message-agent skill.
```

## Sending the Message

For **short, single-line messages**, use `--message` directly:

```bash
uv run mngr message AGENT_NAME --message '[from: my-name] Short message here. To reply, use the /message-agent skill.'
```

For **multiline messages** (which is most messages), write the composed message to a temporary file and use `--message-file`:

```bash
# Write the message to a temp file
# (use the Write tool to create /tmp/mngr-message-AGENT_NAME.txt with the full message content)

# Then send it
uv run mngr message AGENT_NAME --message-file /tmp/mngr-message-AGENT_NAME.txt
```

Prefer `--message-file` for anything longer than a single sentence. It avoids shell quoting issues and preserves formatting.

## After Sending

Report to the user what you sent and to whom. If the send command fails, report the error.
