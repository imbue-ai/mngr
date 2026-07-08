"""OpenCode usage data provider for `mngr usage`.

Single responsibility: install a second in-process TypeScript plugin into each
OpenCode agent's config dir (alongside mngr_opencode's lifecycle plugin) that
appends one ``cost_snapshot`` event per assistant message to
``$MNGR_AGENT_STATE_DIR/events/opencode/usage/events.jsonl``. The ``mngr usage``
CLI walks those events files itself (see ``imbue-mngr-usage``).

Provisioning runs from an ``on_after_provisioning`` hookimpl so the per-agent
OpenCode config dir (and its ``plugin/`` subdir) already exists when we drop our
writer in. All file I/O goes through ``host.write_text_file`` so it works for
local and remote agents uniformly. The reader side is an
``aggregate_usage_source`` hookimpl that claims the ``opencode`` source and
aggregates it with the session-incremental strategy (OpenCode reports cost/tokens
per message, not cumulatively).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode_usage import resources as _resources
from imbue.mngr_usage.api import aggregate_session_incremental
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot

# The usage writer plugin dropped alongside mngr_opencode's lifecycle plugin in
# the per-agent ``<config dir>/plugin/`` (OpenCode auto-loads every plugin/*.ts).
_USAGE_PLUGIN_FILENAME = "mngr_opencode_usage_plugin.ts"

# Source name the writer emits under ($STATE_DIR/events/opencode/usage/...);
# the reader strips "/usage", so this hookimpl claims exactly "opencode".
_OPENCODE_USAGE_SOURCE_NAME = "opencode"


def _load_resource(filename: str) -> str:
    """Load a resource file's text from the mngr_opencode_usage resources package."""
    return importlib.resources.files(_resources).joinpath(filename).read_text()


def _provision_usage_writer_plugin(host: OnlineHostInterface, agent_state_dir: Path) -> None:
    """Install the usage writer plugin into the agent's OpenCode ``plugin/`` dir.

    Idempotent: re-provisioning overwrites the file in place. The destination is
    the same ``plugin/`` dir OpenCode auto-loads, next to mngr_opencode's
    lifecycle plugin.
    """
    config_dir = get_opencode_config_dir(agent_state_dir)
    plugin_dir = get_opencode_plugin_path(config_dir).parent
    host.write_text_file(plugin_dir / _USAGE_PLUGIN_FILENAME, _load_resource(_USAGE_PLUGIN_FILENAME))


@hookimpl
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install the OpenCode usage writer plugin for OpenCode agents on any host.

    Skips non-OpenCode agents. Runs after the harness has provisioned the config
    dir, so the ``plugin/`` dir exists; the writer then loads automatically on the
    next ``opencode serve``.
    """
    if not isinstance(agent, OpenCodeAgent):
        return
    _provision_usage_writer_plugin(host, get_agent_state_dir_path(host.host_dir, agent.id))


@hookimpl
def aggregate_usage_source(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate the OpenCode usage source; decline every other source.

    OpenCode reports each assistant message's own cost/tokens (not a cumulative
    total), so this is the session-incremental strategy. Returning None for
    non-OpenCode sources lets the firstresult hook fall through.
    """
    if source_name != _OPENCODE_USAGE_SOURCE_NAME:
        return None
    return aggregate_session_incremental(source_name, agents_events, since_seconds=since_seconds, now=now)
