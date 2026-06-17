"""pi usage data provider for `mngr usage` (reader + writer gate).

pi loads exactly one explicit extension -- mngr_pi_coding's lifecycle extension,
launched with ``pi -e`` -- so the usage *writer* lives there (it already holds
each assistant message's cost + tokens). This package owns the two pieces that
are genuinely usage-specific:

1. A gate marker, provisioned per pi agent, that tells the lifecycle extension to
   emit usage events. Without it the writer stays inert -- so usage events are
   only written when *this* package (which ships their reader) is installed.
   Emitting them unread would let ``mngr usage`` fall back to the wrong
   (process-cumulative) strategy and undercount pi's per-message events.
2. The reader: an ``aggregate_usage_source`` hookimpl claiming the ``pi-coding``
   source, aggregated session-incrementally (pi reports cost per message).
"""

from __future__ import annotations

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_usage.api import aggregate_session_incremental
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot

# Gate marker the lifecycle extension checks (existsSync) to decide whether to
# emit usage events. Kept in sync with mngr_pi_lifecycle.ts (literal "pi_emit_usage").
_USAGE_GATE_FILENAME = "pi_emit_usage"

# Source the pi writer emits under ($STATE_DIR/events/pi-coding/usage/...); the
# reader strips "/usage". A fixed harness id (not the agent subtype), so usage
# from any pi subtype lumps under one source. Kept in sync with mngr_pi_lifecycle.ts.
_PI_USAGE_SOURCE_NAME = "pi-coding"


@hookimpl
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install the usage gate marker for pi agents so the lifecycle extension emits usage events.

    Skips non-pi agents. Written to the agent state dir, which the extension
    reads (``$MNGR_AGENT_STATE_DIR/pi_emit_usage``) once at startup.
    """
    if not isinstance(agent, PiCodingAgent):
        return
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    host.write_file(state_dir / _USAGE_GATE_FILENAME, b"1")


@hookimpl
def aggregate_usage_source(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate the pi usage source; decline every other source.

    pi reports each assistant message's own cost/tokens (not a cumulative total),
    so this is the session-incremental strategy. Returning None for non-pi sources
    lets the firstresult hook fall through.
    """
    if source_name != _PI_USAGE_SOURCE_NAME:
        return None
    return aggregate_session_incremental(source_name, agents_events, since_seconds=since_seconds, now=now)
