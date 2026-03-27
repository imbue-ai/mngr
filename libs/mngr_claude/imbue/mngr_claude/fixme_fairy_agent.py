from __future__ import annotations

from typing import Final

from pydantic import Field

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude.skill_agent import SkillProvisionedAgent
from imbue.mngr_claude.skill_agent import SkillProvisionedAgentConfig

_SKILL_NAME: Final[str] = "fixme-fairy"

_FIXME_FAIRY_SKILL_CONTENT: Final[str] = """\
---
name: fixme-fairy
description: >
  Find and fix a random FIXME in the codebase. Use when asked to use your primary skill.
---

Important: you are running in a remote sandbox and cannot communicate with the user while you are working through this skill--do NOT ask the user any questions or for any clarifications while working. Instead, do your best to complete the task based on the information you have, and make reasonable assumptions if needed.

# Fixme Fairy: Fix a Random FIXME

Your task is to find and fix ONE random FIXME in this codebase.

## FIXME Format

FIXMEs in this codebase follow this format:

```python
# FIXME(priority)[attempts=N]: (description)
#  (optional additional context)
```

where `description` is a short description of what needs to be fixed, and `N` is the number of prior attempts made to fix it (if any).
If there have been no prior attempts, the `[attempts=N]` part may be omitted.
The priority is simply an integer, with 0 being the highest priority. Priority may or may not be present.
If not present, assume priority=3.

## Step 1: Find a Random FIXME

Run this bash command to select a random FIXME, prioritized by severity:

```bash
./scripts/random_fixme.sh .
```

If no lines are returned, then there are no more remaining FIXMEs, so use your "think-of-something-to-fix" skill to come up with something else to fix instead.

## Step 2: Understand the FIXME

1. Find that FIXME and read the surrounding context (the optional additional context lines below the FIXME line may be important).
2. Gather all the context for the library that contains the FIXME (read CLAUDE.md, docs, style guides, README files, etc.).
3. Think carefully about how best to fix the FIXME.

## Step 3: Fix the FIXME

1. Implement the fix.
2. Run the tests: `uv run pytest`
3. Fix any test failures until all tests pass.

## Step 4: Finalize

If you successfully fixed the FIXME and all tests pass:
1. Remove the FIXME comment (and its additional context lines).
2. Commit your changes.
3. Create a PR titled "fixme-fairy: <short description of the fix>".

If you were unable to fix the FIXME and get all tests passing:
1. Revert any changes you made while attempting the fix.
2. Update the FIXME to increment the attempts count by 1 (if there were no prior attempts, add `[attempts=1]`, if there were some prior attempts, increment the number by 1). The `[attempts=N]` part goes before the `:`, like: `# FIXME[attempts=1]: (description)` or `# FIXME0[attempts=2]: (description)`.
3. Add a brief note to the optional additional context about why you were unable to fix it.
4. Commit this updated FIXME (make sure nothing else is being changed).
5. Create a PR titled "fixme-fairy: FAILED to fix <short description>".
"""


class FixmeFairyAgentConfig(SkillProvisionedAgentConfig):
    """Config for the fixme-fairy agent type."""


class FixmeFairyAgent(SkillProvisionedAgent):
    """Agent implementation for fixme-fairy with skill provisioning."""

    agent_config: FixmeFairyAgentConfig = Field(frozen=True, repr=False, description="Agent type config")

    _skill_name = _SKILL_NAME
    _skill_content = _FIXME_FAIRY_SKILL_CONTENT


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the fixme-fairy agent type."""
    return ("fixme-fairy", FixmeFairyAgent, FixmeFairyAgentConfig)
