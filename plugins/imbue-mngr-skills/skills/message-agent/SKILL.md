---
name: message-agent
argument-hint: <agent_name> <description of what to say>
description: Send a message to another mngr agent. Use when you need to communicate with a peer agent.
allowed-tools: Bash(echo "$MNGR_AGENT_NAME"), Bash(mngr message *), Write(*), Skill(imbue-mngr-skills:find-agent)
---

The user's message contains a target agent name (the first word) and a description of what to communicate. Extract the agent name and treat everything after it as the intent/content of the message.

Your agent name is: !`echo "$MNGR_AGENT_NAME"`

## Agent Name Resolution

Skip this step if the user seems to have provided the exact name of the agent -- just use the first word of their input directly as the target. Only if the input doesn't look like a bare agent name (for example they pasted a branch name like `mngr/foo` or described the agent rather than naming it) should you use the `/imbue-mngr-skills:find-agent` skill to resolve it.

## Composing the Message

Based on the user's description, compose the full message. Every message you send MUST:

1. **Start with a sender tag**: `[from: !`echo "$MNGR_AGENT_NAME"`]`.
2. **Contain the actual content**: Write the message based on what the user described. Be clear and direct.
3. **End with a reply instruction**: Close with a line like: `To reply, use the /imbue-mngr-skills:message-agent skill.`

Example message (for an agent named `refactor-auth`):

```
[from: refactor-auth]

Hey -- I just finished refactoring the auth middleware on my branch. You'll want to rebase before merging since I changed the SessionStore interface. The new method is `get_session_by_token()` instead of `lookup()`.

To reply, use the /imbue-mngr-skills:message-agent skill.
```

## Sending the Message

Write the composed message to a temporary file using the Write tool, then send it with `--message-file`. Name the temp file `/tmp/mngr-message-from-YOUR_NAME-to-AGENT_NAME.txt` (using your agent name and the resolved target name):

```bash
mngr message AGENT_NAME --message-file /tmp/mngr-message-from-YOUR_NAME-to-AGENT_NAME.txt
```

Use `--message-file` for all messages -- it avoids shell quoting issues and preserves formatting.

## After Sending

Report to the user what you sent and to whom. If the send command fails, report the error.
