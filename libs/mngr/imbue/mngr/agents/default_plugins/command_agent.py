from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the ``command`` agent type for running arbitrary shell commands.

    Falls back to ``BaseAgent`` (the ``None`` below): ``assemble_command`` uses
    ``command_override or agent_config.command`` as the base, then appends
    ``cli_args`` and ``agent_args``. That yields
    ``mngr create foo --type command -- <shell command>`` as the basic form
    and lets a reusable custom type pin the base command via
    ``parent_type = "command"`` + ``command = "..."`` in config.

    Arguments after ``--`` are joined with plain spaces to form the agent's
    command, so shell metacharacters like ``&&``, ``|``, or ``;`` must be
    inside a single quoted argument to survive intact to the agent's shell.
    """
    return ("command", None, AgentTypeConfig)
