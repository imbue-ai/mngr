"""mngr_usage plugin entry point.

Agent-agnostic: provides the ``mngr usage`` CLI command. Discovery is by
convention -- the CLI walks ``$MNGR_HOST_DIR/agents/*/events/*/rate_limits/events.jsonl``
matching the same pattern ``mngr transcript`` uses for ``common_transcript``
events. Any plugin that writes ``cost_snapshot`` events to those paths
will be picked up automatically; this plugin doesn't know or care which.
"""

from __future__ import annotations

from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsagePluginConfig

register_plugin_config("usage", UsagePluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the `mngr usage` command."""
    return [usage]
