"""Reading the published agent-skills plugins: the skill files, their frontmatter, and what their examples name.

The skills under each project's ``skills_plugin/`` directory are exported verbatim into the public
``imbue-ai/imbue-skills`` marketplace, where agents copy their examples and run them. The drift
tests (each project's ``test_agent_skills.py``) use these helpers to hold every ``mngr
<subcommand>`` and API path a skill names to the live CLI and schema.
"""

import re
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

MAX_SKILL_BODY_LINES: Final[int] = 500
MAX_SKILL_DESCRIPTION_LENGTH: Final[int] = 1024

_INLINE_CODE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+)`")
_FENCED_CODE: Final[re.Pattern[str]] = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# ``mngr`` followed by whitespace then a word starting with a letter (the
# subcommand). Excludes ``mngr/<branch>``, ``mngr_claude``, ``$MNGR_*``,
# ``mngr --flag``, and path arguments ending in ``/mngr``.
_MNGR_SUBCOMMAND: Final[re.Pattern[str]] = re.compile(r"(?<!/)\bmngr\s+([a-zA-Z][\w-]*)")
_FRONTMATTER_DELIMITER: Final[str] = "---"


class SkillDocument(FrozenModel):
    """One ``SKILL.md``, split into its frontmatter fields and its body."""

    fields: dict[str, str] = Field(description="The frontmatter's key: value pairs")
    body: str = Field(description="Everything after the frontmatter block")


def code_regions(markdown: str) -> list[str]:
    """Return the fenced blocks and inline code spans of a markdown document, prose excluded."""
    return _FENCED_CODE.findall(markdown) + [match.group(1) for match in _INLINE_CODE.finditer(markdown)]


def mngr_subcommands_referenced(markdown: str) -> set[str]:
    """Return every ``mngr <subcommand>`` named inside the document's code regions."""
    return {match.group(1) for region in code_regions(markdown) for match in _MNGR_SUBCOMMAND.finditer(region)}


def skill_files(skills_dir: Path) -> list[Path]:
    return sorted(skills_dir.glob("*/SKILL.md"))


def split_skill_frontmatter(markdown: str) -> SkillDocument:
    """Split a skill into its frontmatter fields and its body.

    The frontmatter is read line by line as ``key: value`` pairs rather than as
    YAML: Claude Code's loader is that lenient (an ``argument-hint`` of
    ``[agent] [instructions...]`` is not valid YAML but is a published skill), and
    a strict parser would reject skills the loader accepts. A document with no
    frontmatter block is reported as having no fields and its whole text as body.
    """
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return SkillDocument(fields={}, body=markdown)
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == _FRONTMATTER_DELIMITER), None
    )
    if closing_index is None:
        return SkillDocument(fields={}, body=markdown)
    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        key, separator, value = line.partition(":")
        if separator and key.strip():
            fields[key.strip()] = value.strip().strip('"')
    return SkillDocument(fields=fields, body="\n".join(lines[closing_index + 1 :]))


def skill_shape_problems(skill_file: Path) -> tuple[str, ...]:
    """The ways one ``SKILL.md`` fails the agentskills.io layout; empty when it conforms."""
    document = split_skill_frontmatter(skill_file.read_text(encoding="utf-8"))
    problems: list[str] = []
    name = document.fields.get("name")
    if name != skill_file.parent.name:
        problems.append(f"frontmatter name {name!r} must equal the directory name {skill_file.parent.name!r}")
    description = document.fields.get("description")
    if description is None:
        problems.append("frontmatter needs a description")
    if description is not None and not 1 <= len(description) <= MAX_SKILL_DESCRIPTION_LENGTH:
        problems.append(f"description is {len(description)} characters; must be 1 to {MAX_SKILL_DESCRIPTION_LENGTH}")
    body_line_count = len(document.body.splitlines())
    if body_line_count > MAX_SKILL_BODY_LINES:
        problems.append(f"body is {body_line_count} lines; must be at most {MAX_SKILL_BODY_LINES}")
    return tuple(problems)
