---
argument-hint: [agent_name] [instructions...]
description: Wait for another agent to enter WAITING state, then execute follow-up instructions
allowed-tools: Bash(uv run mng list *), Bash(while true; do*)
---

The user's message contains an agent name and optional follow-up instructions. Extract the agent name (the first word) and treat everything after it as follow-up instructions.

Note: the user may paste a git branch name like `mng/some-agent` instead of the bare agent name. In that case, strip the `mng/` prefix to get the actual agent name (e.g. `mng/better-tabcomplete` -> `better-tabcomplete`).

## Polling Procedure

First, verify the target agent exists and check its current state (substituting AGENT_NAME with the extracted agent name):

```
uv run mng list --include 'name == "AGENT_NAME"' --format '{name}: {state}'
```

If no output is returned, the agent was not found by that exact name. Run `uv run mng list --format '{name}: {state}'` to see all agents. If the user's input was a description (e.g. "the agent working on X") rather than a name, check the agents and their git branches to figure out which one they meant. If there's an unambiguous match, use it. Otherwise, use AskUserQuestion to ask the user which agent they meant, presenting the plausible candidates.

If the agent is already in WAITING, DONE, or STOPPED state, skip the polling loop and proceed directly to the follow-up task.

Otherwise, poll the agent's lifecycle state every 60 seconds until it leaves the RUNNING state. Run the following bash command (with a 600000ms timeout), substituting AGENT_NAME:

```bash
while true; do
  STATE=$(uv run mng list --include 'name == "AGENT_NAME"' --format '{state}' 2>/dev/null | head -1)
  echo "[$(date '+%H:%M:%S')] Agent 'AGENT_NAME' state: ${STATE:-NOT_FOUND}"
  case "$STATE" in
    WAITING|DONE|STOPPED) echo "Agent 'AGENT_NAME' is ready (state: $STATE)"; break ;;
    "") echo "Agent 'AGENT_NAME' not found, stopping"; break ;;
    *) sleep 60 ;;
  esac
done
```

If this command times out (after 10 minutes), check on the agent by running `tmux capture-pane -t mng-AGENT_NAME -p -S -30` to see its recent output. The tmux session name format is `mng-AGENT_NAME`. If it looks like the agent is still actively working, re-run the polling loop. If it looks stuck or dead, inform the user.

## After the Agent is Ready

Once the agent is in WAITING, DONE, or STOPPED state, carry out the user's follow-up instructions. If no follow-up instructions were provided, inform the user that the agent is ready.
