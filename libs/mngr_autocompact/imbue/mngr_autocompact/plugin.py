from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr_autocompact.cli import autocompact_group
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.manager import compact_agent_if_stale

register_plugin_config("autocompact", AutoCompactPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the autocompact CLI command group."""
    return [autocompact_group]


@hookimpl
def on_before_send_message(agent: AgentInterface, host: HostInterface, message: str) -> None:
    """Trigger context compaction before sending a message if agent is stale (ON_NEXT_PROMPT mode)."""
    if not isinstance(agent, HasCompactionMixin):
        return

    config = agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
    compact_agent_if_stale(agent, config, expected_mode=ContextCompactionMode.ON_NEXT_PROMPT)
