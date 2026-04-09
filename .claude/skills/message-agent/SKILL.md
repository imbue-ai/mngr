---
name: message-agent
argument-hint: <agent_name> <description of what to say>
description: Send a message to another mngr agent. Use when you need to communicate with a peer agent.
allowed-tools: Bash(uv run mngr message *), Bash(cat /tmp/mngr-message-*), Write(*), Skill(find-agent)
---

The user's message contains a target agent name (the first word) and a description of what to communicate. Extract the agent name and treat everything after it as the intent/content of the message.

## Agent Name Resolution

Use the `/find-agent` skill with the first word of the user's input to resolve it to an exact agent name.

## Composing the Message

Based on the user's description, compose the full message. Every message you send MUST:

1. **Start with a sender tag**: `[from: $MNGR_AGENT_NAME]` -- the shell will expand this automatically.
2. **Contain the actual content**: Write the message based on what the user described. Be clear and direct.
3. **End with a reply instruction**: Close with a line like: `To reply, use the /message-agent skill.`

Example message (for an agent named `refactor-auth`):

```
[from: refactor-auth]

Hey -- I just finished refactoring the auth middleware on my branch. You'll want to rebase before merging since I changed the SessionStore interface. The new method is `get_session_by_token()` instead of `lookup()`.

To reply, use the /message-agent skill.
```

## Sending the Message

Always write the message body to a temporary file and use `--message-file`. This avoids shell quoting issues and preserves formatting. Use `$MNGR_AGENT_NAME` in the heredoc so the shell expands it:

```bash
cat > /tmp/mngr-message-AGENT_NAME.txt <<EOF
[from: $MNGR_AGENT_NAME]

<message body here>

To reply, use the /message-agent skill.
EOF
uv run mngr message AGENT_NAME --message-file /tmp/mngr-message-AGENT_NAME.txt
```

Replace `AGENT_NAME` with the resolved target agent name and `<message body here>` with the actual content.

Do NOT use a quoted heredoc delimiter (i.e. do NOT write `<<'EOF'`) -- the delimiter must be unquoted so that `$MNGR_AGENT_NAME` is expanded by the shell.

## After Sending

Report to the user what you sent and to whom (you can `cat /tmp/mngr-message-AGENT_NAME.txt` to confirm the expanded content). If the send command fails, report the error.
