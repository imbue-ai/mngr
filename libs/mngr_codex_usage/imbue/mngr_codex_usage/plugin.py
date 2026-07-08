"""Codex usage data provider for `mngr usage`.

Codex has no statusline or in-process plugin; mngr already tails its rollout
JSONL via a background-tasks supervisor (mngr_codex). This package adds the
usage piece:

1. The writer script ``codex_usage.sh`` -- installed into the agent's
   ``commands/`` dir, where mngr_codex's ``codex_background_tasks.sh`` launches it
   *iff present* (so usage events are only written when this package, which ships
   their reader, is installed). It reads the raw rollout stream and emits one
   ``cost_snapshot`` per ``token_count`` item (cumulative tokens + rate-limit
   windows; no dollar cost -- Codex reports none).
2. The reader: an ``aggregate_usage_source`` hookimpl claiming the ``codex``
   source, aggregated session-cumulatively (each token_count carries the
   session's cumulative total). Cost is estimated from tokens via the pricing
   table; the 5h/7d windows are surfaced too.
"""

from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.hosts.host import install_packaged_script_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex_usage import resources as _resources
from imbue.mngr_usage.api import aggregate_session_cumulative
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot

# Writer script dropped into the agent's commands/ dir; codex_background_tasks.sh
# launches it when present. Kept in sync with that supervisor's USAGE_SCRIPT path.
_USAGE_WRITER_SCRIPT = "codex_usage.sh"

# The Python emitter the writer script invokes (python3 <dir>/codex_usage_emit.py).
# Installed next to the writer so the writer resolves it relative to itself.
_USAGE_EMIT_SCRIPT = "codex_usage_emit.py"

# Source the writer emits under ($STATE_DIR/events/codex/usage/...); the reader
# strips "/usage", so this hookimpl claims exactly "codex".
_CODEX_USAGE_SOURCE_NAME = "codex"


@hookimpl
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install the Codex usage writer into the agent's commands/ dir (executable).

    Skips non-Codex agents. The background-tasks supervisor (already provisioned
    by mngr_codex) launches it on the next agent start because it is present.
    """
    if not isinstance(agent, CodexAgent):
        return
    commands_dir = get_agent_state_dir_path(host.host_dir, agent.id) / "commands"
    install_packaged_script_on_host(
        host,
        module=_resources,
        filename=_USAGE_WRITER_SCRIPT,
        dest=commands_dir / _USAGE_WRITER_SCRIPT,
    )
    install_packaged_script_on_host(
        host,
        module=_resources,
        filename=_USAGE_EMIT_SCRIPT,
        dest=commands_dir / _USAGE_EMIT_SCRIPT,
    )


@hookimpl
def aggregate_usage_source(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate the Codex usage source; decline every other source.

    Codex's ``token_count`` carries the session's cumulative token total, so this
    is the session-cumulative strategy. Returning None for non-Codex sources lets
    the firstresult hook fall through.
    """
    if source_name != _CODEX_USAGE_SOURCE_NAME:
        return None
    return aggregate_session_cumulative(source_name, agents_events, since_seconds=since_seconds, now=now)
