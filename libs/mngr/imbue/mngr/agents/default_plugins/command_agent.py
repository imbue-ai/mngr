from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import GenericCommandAgentMixin


class CommandAgent(BaseAgent[AgentTypeConfig], GenericCommandAgentMixin):
    """Agent type that runs an arbitrary shell command (see ``register_agent_type``).

    Adds nothing to ``BaseAgent`` beyond the ``GenericCommandAgentMixin`` marker, which
    records that this is a bare command runner (not a CLI-backed agent) and that it runs
    unattended by nature.
    """


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the ``command`` agent type for running arbitrary shell commands.

    ``assemble_command`` uses ``command_override or agent_config.command`` as the base,
    then appends ``cli_args`` and ``agent_args``. That yields
    ``mngr create foo --type command -- <shell command>`` as the basic form
    and lets a reusable custom type pin the base command via
    ``parent_type = "command"`` + ``command = "..."`` in config.

    Arguments after ``--`` are joined with plain spaces to form the agent's
    command, so shell metacharacters like ``&&``, ``|``, or ``;`` must be
    inside a single quoted argument to survive intact to the agent's shell.
    """
    return ("command", CommandAgent, AgentTypeConfig)
