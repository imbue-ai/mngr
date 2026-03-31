"""System prompt plugin for the webchat server.

Assembles a system prompt from markdown files in the agent's working
directory (``GLOBAL.md`` and ``talking/PROMPT.md``) and injects it into
the ``llm`` CLI command via the ``modify_llm_prompt_command`` hook.

Reads the files once at startup and passes the assembled prompt directly
via ``--system``.
"""

from __future__ import annotations

from pathlib import Path

from llm_webchat.hookspecs import hookimpl
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


def _build_system_prompt(agent_work_dir: Path) -> str | None:
    """Assemble a system prompt from GLOBAL.md and talking/PROMPT.md.

    Returns the concatenated content, or None if neither file exists.
    """
    parts: list[str] = []

    global_md = agent_work_dir / "GLOBAL.md"
    if global_md.is_file():
        try:
            parts.append(global_md.read_text())
        except OSError as exc:
            logger.warning("Failed to read {}: {}", global_md, exc)

    talking_prompt = agent_work_dir / "talking" / "PROMPT.md"
    if talking_prompt.is_file():
        try:
            parts.append(talking_prompt.read_text())
        except OSError as exc:
            logger.warning("Failed to read {}: {}", talking_prompt, exc)

    if not parts:
        return None

    return "\n\n".join(parts)


def _command_has_system_prompt(command: list[str]) -> bool:
    """Return True if the command already contains a --system or -s flag."""
    return "--system" in command or "-s" in command


class SystemPromptPlugin(FrozenModel):
    """Pluggy plugin that injects a system prompt assembled from agent markdown files.

    The system prompt is built once at construction time from the agent's
    working directory. On each ``modify_llm_prompt_command`` call, if the
    command does not already contain a ``--system`` flag, the assembled
    prompt is injected.
    """

    system_prompt: str | None = Field(
        default=None,
        description="Pre-assembled system prompt text, or None if no prompt files were found",
    )

    @hookimpl
    def modify_llm_prompt_command(self, command: list[str]) -> None:
        if self.system_prompt is None:
            return
        if _command_has_system_prompt(command):
            return
        command.extend(["--system", self.system_prompt])


def create_system_prompt_plugin(agent_work_dir: str) -> SystemPromptPlugin | None:
    """Create a SystemPromptPlugin from the agent working directory.

    Returns None if the working directory is empty or no prompt files exist.
    """
    if not agent_work_dir:
        logger.debug("MNGR_AGENT_WORK_DIR not set, system prompt plugin will not be registered")
        return None

    system_prompt = _build_system_prompt(Path(agent_work_dir))
    if system_prompt is None:
        logger.debug("No system prompt files found in {}", agent_work_dir)
        return None

    logger.info("Built system prompt from {} ({} chars)", agent_work_dir, len(system_prompt))
    return SystemPromptPlugin(system_prompt=system_prompt)
