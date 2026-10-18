"""Guard the published ``imbue-mngr-skills`` plugin against drift from the live CLI.

The skills under ``libs/mngr/skills_plugin/`` are exported verbatim into the public
``imbue-ai/imbue-skills`` marketplace, where agents copy their ``mngr ...`` examples
and run them. A renamed or removed subcommand would go stale there silently, so
every ``mngr <subcommand>`` token inside a code span or fenced block must name a
command on the CLI this checkout builds, and every skill must keep the shape the
agentskills.io layout (and Claude Code's loader) require.
"""

from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.main import cli
from imbue.mngr.utils.agent_skills import mngr_subcommands_referenced
from imbue.mngr.utils.agent_skills import skill_files
from imbue.mngr.utils.agent_skills import skill_shape_problems

SKILLS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "skills_plugin" / "skills"


def _skill_ids() -> list[str]:
    return [path.parent.name for path in skill_files(SKILLS_DIR)]


def test_plugin_directory_has_the_published_shape() -> None:
    plugin_dir = SKILLS_DIR.parent
    assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()
    assert (plugin_dir / "README.md").is_file()
    # The four skills this plugin has always carried; a vacuous scan would mean the layout moved.
    assert _skill_ids() == ["find-agent", "message-agent", "mngr-help", "wait-for-agent"]


@pytest.mark.parametrize("skill_name", _skill_ids())
def test_skill_has_valid_shape(skill_name: str) -> None:
    assert skill_shape_problems(SKILLS_DIR / skill_name / "SKILL.md") == ()


@pytest.mark.parametrize("skill_name", _skill_ids())
def test_skill_mngr_subcommands_exist_on_the_live_cli(skill_name: str) -> None:
    referenced = mngr_subcommands_referenced((SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8"))
    missing = referenced - set(cli.commands)
    assert not missing, f"{skill_name} names mngr subcommands the live CLI does not have: {sorted(missing)}"
