---
name: "find-agent"
description: "Resolve an agent name or description to an exact mngr agent name. Used by other skills that target agents."
---

## Invocation Input

Invoke this skill with an agent name, branch name, or description as the input text.

## Tool Guidance

The source Claude skill limited shell access to `uv run mngr list`. Codex skills do not enforce that as a permission boundary, so keep shell usage scoped to agent-name resolution.

The user's input is an agent name or description. Resolve it to an exact agent name.

## Normalization

The user may paste a git branch name like `mngr/some-agent` instead of the bare agent name. In that case, strip the `mngr/` prefix to get the actual agent name (e.g. `mngr/better-tabcomplete` -> `better-tabcomplete`).

## Resolution

Verify the target agent exists by running:

```
uv run mngr list --format '{name}'
```

If the extracted name doesn't match any agent exactly, check if the user's input was a description (e.g. "the agent working on X") rather than a name, and try to match against the listed agents and their git branches. If there's an unambiguous match, use it. Otherwise, use AskUserQuestion to ask the user which agent they meant, presenting the plausible candidates.

Report the resolved agent name back to the caller.
