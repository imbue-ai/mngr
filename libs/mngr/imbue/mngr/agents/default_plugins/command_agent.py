from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString


class CommandAgentConfig(AgentTypeConfig):
    """Config for the ``command`` agent type."""


class CommandAgent(BaseAgent[CommandAgentConfig]):
    """Agent type whose command comes from ``agent_config.command`` and/or the CLI args after ``--``.

    Used when the caller wants to run an arbitrary shell command without
    registering a dedicated agent type. The final command is
    ``{agent_config.command} {agent_args}`` joined with plain spaces, e.g.::

        mngr create my-task --type command -- sleep 99999
        mngr create my-task --type command -- 'echo hi && sleep 60'

    A custom type can also pin the base command in config::

        [agent_types.my_server]
        parent_type = "command"
        command = "python -m http.server 8080"

    Then ``mngr create web my_server`` runs ``python -m http.server 8080``,
    and ``mngr create web my_server -- --bind 0.0.0.0`` runs
    ``python -m http.server 8080 --bind 0.0.0.0``.

    At least one of ``agent_config.command`` or ``agent_args`` must be set.
    Because args are joined with plain spaces, shell metacharacters like
    ``&&``, ``|``, or ``;`` must be inside a single quoted argument so
    that they survive intact to the agent's shell.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        if command_override is not None:
            return command_override
        base = str(self.agent_config.command) if self.agent_config.command is not None else None
        if base is None and not agent_args:
            raise UserInputError(
                "--type command requires a command after `--` (or `command = ...` set on the agent type), "
                "e.g. `mngr create foo --type command -- sleep 99999`"
            )
        parts: list[str] = []
        if base is not None:
            parts.append(base)
        if self.agent_config.cli_args:
            parts.extend(self.agent_config.cli_args)
        parts.extend(agent_args)
        return CommandString(" ".join(parts))


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the ``command`` agent type."""
    return ("command", CommandAgent, CommandAgentConfig)
