"""Plugin entry point: registers the ``mngr latchkey`` CLI group, config block and host-creation hook."""

from collections.abc import Sequence

import click

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_latchkey import hookimpl
from imbue.mngr_latchkey.agent_setup import CONTAINER_HOSTS_FILE_PATH
from imbue.mngr_latchkey.agent_setup import fall_back_to_reverse_tunneled_gateway_url
from imbue.mngr_latchkey.cli import latchkey as latchkey_group
from imbue.mngr_latchkey.config import LatchkeyPluginConfig

register_plugin_config("latchkey", LatchkeyPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level ``mngr latchkey`` command group."""
    return [latchkey_group]


@hookimpl
def on_host_created(host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Keep a new host's ``LATCHKEY_GATEWAY`` reachable from its container before any agent starts."""
    del mngr_ctx
    fall_back_to_reverse_tunneled_gateway_url(host, CONTAINER_HOSTS_FILE_PATH)
