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

from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.interfaces.help_topic import DocFile
from imbue.mngr.interfaces.help_topic import TopicHelpPage
from imbue.mngr.interfaces.help_topic import imbue_mngr_doc_url
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsagePluginConfig

# Docs root resolution, mirroring mngr's built-in topics. In a wheel the docs are
# force-included under the package at imbue/mngr_usage/docs; in a source/editable
# checkout they live at libs/mngr_usage/docs (this file is
# .../imbue/mngr_usage/plugin.py). Prefer the packaged copy, else the source tree.
# _PACKAGED_DOCS_DIR is imbue/mngr_usage/docs (force-included in the wheel);
# _SOURCE_DOCS_DIR is libs/mngr_usage/docs (the source/editable checkout).
_PACKAGED_DOCS_DIR = Path(__file__).resolve().parent / "docs"
_SOURCE_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
_DOCS_DIR = _PACKAGED_DOCS_DIR if _PACKAGED_DOCS_DIR.is_dir() else _SOURCE_DOCS_DIR

register_plugin_config("usage", UsagePluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the `mngr usage` command."""
    return [usage]


@hookimpl
def register_help_topics() -> Sequence[TopicHelpPage]:
    """Expose this plugin's docs as `mngr help` topics.

    Keys and descriptions are namespaced (``usage_`` key prefix, ``mngr usage:``
    description prefix) so they are unambiguous in the global `mngr help` topic
    list. The body is the markdown doc file, rendered at display time; the file
    itself keeps its plain name/heading, which read fine in its own
    mngr_usage/docs context.
    """
    return [
        TopicHelpPage(
            key="usage_cron_recipes",
            one_line_description="mngr usage: Cron automation recipes",
            docs_path="cron_recipes.md",
            body=DocFile(
                path=_DOCS_DIR / "cron_recipes.md",
                source_url=imbue_mngr_doc_url("libs/mngr_usage/docs/cron_recipes.md"),
            ),
        ),
    ]
