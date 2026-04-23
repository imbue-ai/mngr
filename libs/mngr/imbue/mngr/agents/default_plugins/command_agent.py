from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface


class CommandAgentConfig(AgentTypeConfig):
    """Config for the ``command`` agent type."""


class CommandAgent(BaseAgent[CommandAgentConfig]):
    """Agent type for running arbitrary shell commands.

    Entirely inherits ``BaseAgent.assemble_command``: the command is
    ``{base} {cli_args} {agent_args}`` where ``base`` is ``command_override``
    or ``agent_config.command`` (or omitted when the whole command lives in
    ``agent_args``). Exists as a registered type so callers have a clear
    ``--type command --`` invocation for shell commands without registering
    a dedicated type. E.g.::

        mngr create my-task --type command -- sleep 99999
        mngr create my-task --type command -- 'echo hi && sleep 60'

    A custom type can also pin the base command in config::

        [agent_types.my_server]
        parent_type = "command"
        command = "python -m http.server 8080"

    Then ``mngr create my-task my_server`` runs ``python -m http.server 8080``,
    and ``mngr create my-task my_server -- --bind 0.0.0.0`` runs
    ``python -m http.server 8080 --bind 0.0.0.0``.

    Because args are joined with plain spaces, shell metacharacters like
    ``&&``, ``|``, or ``;`` must be inside a single quoted argument so
    that they survive intact to the agent's shell.
    """


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the ``command`` agent type."""
    return ("command", CommandAgent, CommandAgentConfig)
