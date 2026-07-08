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
from types import ModuleType

import click
from loguru import logger

from imbue.mngr import hookimpl
from imbue.mngr.cli.doc_links import imbue_mngr_doc_url
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.help_topic import DocFile
from imbue.mngr.interfaces.help_topic import TopicHelpPage
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr_usage import hookspecs as usage_hookspecs
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.preservation import preserve_agent_usage

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
def register_hookspecs() -> ModuleType:
    """Contribute the ``aggregate_usage_source`` reader hookspec so usage plugins can ship readers."""
    return usage_hookspecs


def _is_preserve_on_destroy_enabled(mngr_ctx: MngrContext) -> bool:
    """Whether usage events should be preserved on destroy (from plugin config)."""
    return mngr_ctx.get_plugin_config("usage", UsagePluginConfig).preserve_on_destroy


def _preserve_destroyed_agent_usage(
    source: HostFileReadInterface,
    host: HostInterface,
    agent_name: AgentName,
    agent_id: AgentId,
    provider_name: str,
    mngr_ctx: MngrContext,
) -> None:
    """Preserve one destroyed agent's usage events, capturing its host metadata.

    Best-effort: any failure is logged and swallowed. These run from the
    ``on_before_*_destroy`` hooks, whose contract is that a raised exception
    *aborts the destroy* -- preservation must never be able to block teardown.
    """
    try:
        preserve_agent_usage(
            source,
            get_agent_state_dir_path(host.host_dir, agent_id),
            agent_name,
            agent_id,
            provider_name=provider_name,
            host_id=str(host.id),
            host_name=str(host.get_name()),
            mngr_ctx=mngr_ctx,
        )
    except (MngrError, OSError) as e:
        logger.warning("Failed to preserve usage for agent {} on destroy: {}", agent_id, e)


@hookimpl
def on_before_agent_destroy(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Preserve the agent's usage events before its online state directory is deleted.

    Agent-agnostic: fires for every agent type, but :func:`preserve_agent_usage`
    is a no-op for agents that wrote no usage events, so only usage writers (e.g.
    Claude agents via ``mngr_claude_usage``) actually produce a preserved copy.
    ``provider_name`` is read from the agent's discovery record (the only place
    that carries it without reaching into a concrete host implementation).

    Best-effort: a failure resolving the provider or preserving is logged and
    swallowed -- this hook must never raise, since a raise aborts the destroy.
    """
    mngr_ctx = agent.mngr_ctx
    if not _is_preserve_on_destroy_enabled(mngr_ctx):
        return
    try:
        provider_name = next(
            (str(ref.provider_name) for ref in host.discover_agents() if ref.agent_id == agent.id),
            None,
        )
    except (MngrError, OSError) as e:
        logger.warning("Could not discover agents to preserve usage for {}: {}", agent.id, e)
        return
    if provider_name is None:
        logger.debug("Could not resolve provider for agent {}; skipping usage preservation", agent.id)
        return
    _preserve_destroyed_agent_usage(host, host, agent.name, agent.id, provider_name, mngr_ctx)


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve usage events from a host's volume before the host is destroyed.

    When a host is destroyed without each agent's ``on_before_agent_destroy``
    firing, usage data still lives on the host's persisted volume. If the
    provider surfaces that volume the host is a :class:`HostFileReadInterface`,
    so the same :func:`preserve_agent_usage` reads the files straight off it. If
    the host is not readable (no volume), there is nothing we can preserve.

    Best-effort: discovery and per-agent preservation failures are logged and
    swallowed -- this hook must never raise, since a raise aborts the destroy.
    """
    if not _is_preserve_on_destroy_enabled(mngr_ctx):
        return
    if not isinstance(host, HostFileReadInterface):
        logger.debug("Host {} is not readable (no volume); skipping usage preservation", host.id)
        return
    try:
        refs = host.discover_agents()
    except (MngrError, OSError) as e:
        logger.warning("Could not discover agents on host {} to preserve usage: {}", host.id, e)
        return
    for ref in refs:
        _preserve_destroyed_agent_usage(host, host, ref.agent_name, ref.agent_id, str(ref.provider_name), mngr_ctx)


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
