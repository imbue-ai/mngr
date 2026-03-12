from __future__ import annotations

import shlex

from imbue.mng import hookimpl
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_claude_mind.plugin import ClaudeMindAgent
from imbue.mng_claude_mind.plugin import ClaudeMindConfig

ELENA_SYSTEM_PROMPT = "You are Elena, an assistant powered by Claude Code that helps users write software."


_APPEND_SYSTEM_PROMPT_FLAG = "--append-system-prompt"


def _merge_system_prompt_into_args(elena_prompt: str, agent_args: tuple[str, ...]) -> tuple[str, ...]:
    """Merge Elena's system prompt with any existing --append-system-prompt in agent_args.

    If --append-system-prompt already exists in agent_args, the prompts are merged
    (newline-separated, Elena's first) into a single flag value. Otherwise the
    flag is prepended.

    Handles both ``--append-system-prompt VALUE`` (two tokens) and
    ``--append-system-prompt=VALUE`` (single token) forms.
    """
    flag = _APPEND_SYSTEM_PROMPT_FLAG
    args_list = list(agent_args)

    for i, arg in enumerate(args_list):
        if arg == flag and i + 1 < len(args_list):
            existing_quoted = args_list[i + 1]
            existing_unquoted = _shell_unquote(existing_quoted)
            merged = elena_prompt + "\n" + existing_unquoted
            args_list[i + 1] = shlex.quote(merged)
            return tuple(args_list)

        if arg.startswith(flag + "="):
            existing_quoted = arg[len(flag) + 1 :]
            existing_unquoted = _shell_unquote(existing_quoted)
            merged = elena_prompt + "\n" + existing_unquoted
            args_list[i] = flag + "=" + shlex.quote(merged)
            return tuple(args_list)

    return (flag, shlex.quote(elena_prompt)) + agent_args


def _shell_unquote(value: str) -> str:
    """Unquote a possibly shell-quoted string.

    Uses shlex POSIX-mode splitting to strip surrounding quotes. Returns
    the original value unchanged if parsing fails or yields no tokens.
    """
    try:
        tokens = shlex.split(value)
    except ValueError:
        return value
    if len(tokens) == 1:
        return tokens[0]
    return value


class ElenaCodeAgent(ClaudeMindAgent):
    """A conversational AI mind agent powered by Claude Code.

    Elena is designed to be purely conversational -- she interacts with users
    via a web-accessible Claude Code session but is instructed to never write
    code or modify files. Her system prompt encourages friendly, thoughtful
    conversation.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Assemble command with Elena's system prompt merged into agent args.

        If --append-system-prompt is already present in agent_args, the prompts
        are merged (newline-separated) into a single flag value. Otherwise the
        flag is added.
        """
        merged_args = _merge_system_prompt_into_args(ELENA_SYSTEM_PROMPT, agent_args)
        # FOLLOWUP: we have to remove this!!!  There are lots of permissions prompts to work through though
        if "--dangerously-skip-permissions" not in merged_args:
            merged_args += ("--dangerously-skip-permissions",)
        return super().assemble_command(host, merged_args, command_override)


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the elena-code agent type."""
    return ("elena-code", ElenaCodeAgent, ClaudeMindConfig)
