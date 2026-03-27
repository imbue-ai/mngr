---
argument-hint: [agent_name] [instructions...]
description: Wait for another agent to enter WAITING state, then execute follow-up instructions
allowed-tools: Bash(uv run mngr list *), Bash(while true; do*)
---

The user's message contains an agent name and optional follow-up instructions. Extract the agent name (the first word) and treat everything after it as follow-up instructions.

Note: the user may paste a git branch name like `mngr/some-agent` instead of the bare agent name. In that case, strip the `mngr/` prefix to get the actual agent name (e.g. `mngr/better-tabcomplete` -> `better-tabcomplete`).

## Agent Name Resolution

First, verify the target agent exists by running:

```
uv run mngr list --format '{name}'
```

If the extracted name doesn't match any agent exactly, check if the user's input was a description (e.g. "the agent working on X") rather than a name, and try to match against the listed agents and their git branches. If there's an unambiguous match, use it. Otherwise, use AskUserQuestion to ask the user which agent they meant, presenting the plausible candidates.

## Polling

Run the following bash command (with a 600000ms timeout), substituting AGENT_NAME with the resolved agent name. The loop returns immediately if the agent is already in a ready state:

```bash
while true; do
  OUTPUT=$(uv run mngr list --include 'name == "AGENT_NAME"' --format '{state}|{plugin.claude.waiting_reason}' 2>/dev/null | head -1)
  STATE="${OUTPUT%%|*}"
  REASON="${OUTPUT#*|}"
  echo "[$(date '+%H:%M:%S')] Agent 'AGENT_NAME' state: ${STATE:-NOT_FOUND} (reason: ${REASON:-none})"
  case "$STATE" in
    DONE|STOPPED) echo "Agent 'AGENT_NAME' is ready (state: $STATE)"; break ;;
    WAITING)
      if [ "$REASON" = "PERMISSIONS" ]; then
        echo "Agent 'AGENT_NAME' waiting on permissions, continuing to poll..."
        sleep 60
      else
        echo "Agent 'AGENT_NAME' is ready (state: $STATE)"; break
      fi ;;
    "") echo "Agent 'AGENT_NAME' not found, stopping"; break ;;
    *) sleep 60 ;;
  esac
done
```

If this command times out (after 10 minutes), check on the agent by running `tmux capture-pane -t mngr-AGENT_NAME -p -S -30` to see its recent output. The tmux session name format is `mngr-AGENT_NAME`. If it looks like the agent is still actively working, re-run the polling loop. If it looks stuck or dead, inform the user.

## After the Agent is Ready

Once the agent is in WAITING (without a permissions reason), DONE, or STOPPED state, carry out the user's follow-up instructions. If no follow-up instructions were provided, inform the user that the agent is ready.
