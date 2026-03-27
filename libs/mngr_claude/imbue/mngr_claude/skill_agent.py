"""Shared base for agent types that provision a Claude skill."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig


class SkillProvisionedAgentConfig(ClaudeAgentConfig):
    """Config for agent types that provision a Claude skill.

    Subclass this for each skill-provisioned agent type to get a distinct
    config class for the agent registry.
    """


def _prompt_user_for_skill_install(skill_name: str, skill_path: Path) -> bool:
    """Prompt the user to install or update a skill."""
    if skill_path.exists():
        logger.info(
            "\nThe {} skill at {} will be updated.\n",
            skill_name,
            skill_path,
        )
        return click.confirm(f"Update the {skill_name} skill?", default=True)
    else:
        logger.info(
            "\nThe {} skill will be installed to {}.\n",
            skill_name,
            skill_path,
        )
        return click.confirm(f"Install the {skill_name} skill?", default=True)


def _install_skill_locally(skill_name: str, skill_content: str, mngr_ctx: MngrContext) -> None:
    """Install a skill to the local user's ~/.claude/skills/."""
    skill_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"

    with log_span("Installing {} skill to {}", skill_name, skill_path):
        # Skip if the skill is already installed with the same content
        if skill_path.exists() and skill_path.read_text() == skill_content:
            logger.debug("{} skill is already up to date at {}", skill_name, skill_path)
            return

        if mngr_ctx.is_interactive and not mngr_ctx.is_auto_approve:
            if not _prompt_user_for_skill_install(skill_name, skill_path):
                logger.info("Skipped {} skill installation", skill_name)
                return

        atomic_write(skill_path, skill_content)
        logger.debug("Installed {} skill to {}", skill_name, skill_path)


def _install_skill_remotely(skill_name: str, skill_content: str, host: OnlineHostInterface) -> None:
    """Install a skill on a remote host."""
    skill_path = Path(f".claude/skills/{skill_name}/SKILL.md")

    with log_span("Installing {} skill on remote host", skill_name):
        host.execute_idempotent_command(
            f"mkdir -p ~/.claude/skills/{skill_name}",
            timeout_seconds=10.0,
        )
        host.write_text_file(skill_path, skill_content)
        logger.debug("Installed {} skill on remote host", skill_name)


class SkillProvisionedAgent(ClaudeAgent):
    """Base agent that provisions a Claude skill during setup.

    Subclasses must set the _skill_name and _skill_content class variables
    to define which skill to install.
    """

    agent_config: SkillProvisionedAgentConfig = Field(frozen=True, repr=False, description="Agent type config")

    _skill_name: ClassVar[str]
    _skill_content: ClassVar[str]

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Run standard Claude provisioning, then install the skill."""
        super().provision(host, options, mngr_ctx)

        if host.is_local:
            _install_skill_locally(self._skill_name, self._skill_content, mngr_ctx)
        else:
            _install_skill_remotely(self._skill_name, self._skill_content, host)
