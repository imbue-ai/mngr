"""mngr_usage plugin entry point.

Agent-agnostic: provides the ``mngr usage`` CLI command. Discovery is by
convention -- the CLI walks ``$MNGR_HOST_DIR/agents/*/events/*/usage/events.jsonl``
matching the same pattern ``mngr transcript`` uses for ``common_transcript``
events. Any plugin that writes ``cost_snapshot`` events to those paths
will be picked up automatically; this plugin doesn't know or care which.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import click

from imbue.imbue_common.model_update import to_update
from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.interfaces.help_topic import TopicHelpPage
from imbue.mngr.interfaces.help_topic import build_topics_from_directory
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsagePluginConfig

# The plugin's markdown docs live at libs/mngr_usage/docs (a sibling of the
# imbue/ package root). This file is libs/mngr_usage/imbue/mngr_usage/plugin.py,
# so the docs directory is three parents up. Mirrors how mngr locates its own
# built-in topic docs; if the docs are absent (e.g. a wheel install that doesn't
# ship them) build_topics_from_directory simply returns no topics.
_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"

register_plugin_config("usage", UsagePluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the `mngr usage` command."""
    return [usage]


@hookimpl
def register_help_topics() -> Sequence[TopicHelpPage]:
    """Expose this plugin's markdown docs (e.g. cron_recipes) as `mngr help` topics.

    Topic keys and descriptions are namespaced (``usage_`` key prefix, ``mngr
    usage:`` description prefix) so they are unambiguous in the global `mngr
    help` topic list -- e.g. the docs/cron_recipes.md file surfaces as
    ``usage_cron_recipes`` -- "mngr usage: Cron automation recipes". The doc
    file itself keeps its plain name/heading, which read fine in its own
    mngr_usage/docs context.
    """
    return [
        topic.model_copy_update(
            to_update(topic.field_ref().key, f"usage_{topic.key}"),
            to_update(topic.field_ref().one_line_description, f"mngr usage: {topic.one_line_description}"),
        )
        for topic in build_topics_from_directory("usage", _DOCS_DIR)
    ]
