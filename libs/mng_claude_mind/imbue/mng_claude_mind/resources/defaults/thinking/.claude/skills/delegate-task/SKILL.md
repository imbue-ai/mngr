---
name: delegate-task
description: Create a sub-agent to perform a task. Use when you need to delegate work to another agent, for example, a working or verifying agent.
---

# Delegating tasks to sub-agents

As the thinking agent, you should NEVER do work directly. Instead, delegate all tasks to sub-agents via `mng create`.

## Creating a working agent

To delegate a task, create a sub-agent using `mng`:

```bash
mng create <task-name> --message "Your task instructions here"
```

The `<task-name>` should be a descriptive name for the task (e.g. `fix-login-bug`, `add-search-feature`).
Note that the names must be unique because git branches are created for each task.
If the command fails because the name is taken, simply choose a more specific, longer name.

The `--message` flag sends an initial prompt to the agent describing what work to do. Be specific and include:
- What the task is and why it needs to be done
- Any relevant context (e.g. related conversation IDs, prior attempts, constraints)
- Success criteria so the agent (and later the verifier) knows what "done" looks like

## Creating a verifying agent

When a working agent finishes (you will receive an `mng/agents` event), create a verifying agent to check the work:

```bash
mng create verify-<task-name> --message "Verify that the following task was completed successfully: <description>. The agent that performed the work was <agent-name>. Check <specific things to verify>."
```

## Useful mng commands

- `mng list` - see all running agents and their states
- `mng message <agent> -m "..."` - send a follow-up message to an agent
- `mng destroy <agent>` - clean up a finished or failed agent
- `mng exec <agent> "command"` - run a shell command on an agent's host
- `mng connect <agent>` - attach to an agent's terminal

## Guidelines

- Always give tasks clear, descriptive names so they are easy to track.
- Always include success criteria in your task instructions.
- When a task fails or crashes, review the error before retrying. Consider whether the instructions need to be revised.
- Clean up finished agents with `mng destroy` after you have processed their results.
- If the user should be able to see the task in their agent list, use `mng`. For quick internal operations that are not user-visible, consider using your own sub-agents instead.
