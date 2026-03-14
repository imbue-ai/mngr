from __future__ import annotations

from typing import Final

from pydantic import Field

from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng_claude import hookimpl
from imbue.mng_claude.skill_agent import SkillProvisionedAgent
from imbue.mng_claude.skill_agent import SkillProvisionedAgentConfig

_SKILL_NAME: Final[str] = "code-guardian"

_CODE_GUARDIAN_SKILL_CONTENT: Final[str] = """\
---
name: code-guardian
description: >
  Identify the most important code-level inconsistencies in the codebase and
  produce a structured report. Use when asked to use your primary skill.
---

Important: you are running in a remote sandbox and cannot communicate with the user while you are working through this skill--do NOT ask the user any questions or for any clarifications while compiling the report.  Instead, do your best to complete the task based on the information you have, and make reasonable assumptions if needed.

# Code Guardian: Identify Inconsistencies

Your task is to identify the most important code-level inconsistencies in this codebase.

## Instructions

1. Read through the codebase documentation (CLAUDE.md, README files, style guides, etc.)
   to understand the project's conventions and architecture.
2. Read non_issues.md if it exists -- do NOT report anything listed there.
3. Review the code and identify inconsistencies:
   - Things done in different ways in different places
   - Inconsistent variable/function/class naming
   - Pattern violations and style guide deviations
   - Any other code-level inconsistencies
4. Do NOT worry about docstrings, comments, or documentation (those are covered separately).
5. Do NOT worry about inconsistencies between docs/specs and code (covered separately).
6. Do NOT report issues already covered by an existing FIXME.

## Output

Put the inconsistencies, in order from most important to least important, into a markdown
file at `_tasks/inconsistencies/<date>.md` (create the directory if needed).

Get the date by running: `date +%Y-%m-%d-%T | tr : -`

Use this format:

```markdown
# Inconsistencies identified on <date>

## 1. <Short description>

Description: <detailed description with file names and line numbers>

Recommendation: <recommendation for fixing>

Decision: Accept
```

Then commit the file and either update (if it exists) or create (if it does not exist) a PR titled "code-guardian: inconsistency report".
"""


class CodeGuardianAgentConfig(SkillProvisionedAgentConfig):
    """Config for the code-guardian agent type."""


class CodeGuardianAgent(SkillProvisionedAgent):
    """Agent implementation for code-guardian with skill provisioning."""

    agent_config: CodeGuardianAgentConfig = Field(frozen=True, repr=False, description="Agent type config")

    _skill_name = _SKILL_NAME
    _skill_content = _CODE_GUARDIAN_SKILL_CONTENT


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the code-guardian agent type."""
    return ("code-guardian", CodeGuardianAgent, CodeGuardianAgentConfig)
