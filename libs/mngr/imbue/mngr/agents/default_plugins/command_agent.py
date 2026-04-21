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
    """Agent type whose command is whatever comes after ``--`` on the CLI.

    Used when the caller wants to run an arbitrary shell command without
    registering a dedicated agent type. Everything after ``--`` is joined
    with plain spaces (no shell-quoting) and executed as the agent's main
    command, e.g.::

        mngr create my-task --type command -- sleep 99999
        mngr create my-task --type command -- 'echo hi && sleep 60'

    Because args are joined with plain spaces, shell metacharacters like
    ``&&``, ``|``, or ``;`` must be inside a single quoted argument so
    that they survive intact to the agent's shell. The stored command
    string is executed by the agent's outer shell, so there is no need
    to wrap it in ``sh -c`` yourself.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        if command_override is not None:
            return command_override
        if not agent_args:
            raise UserInputError(
                "--type command requires a command after `--`, e.g. `mngr create foo --type command -- sleep 99999`"
            )
        return CommandString(" ".join(agent_args))


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the ``command`` agent type."""
    return ("command", CommandAgent, CommandAgentConfig)
