"""Reader hookspec contributed by ``mngr_usage`` via ``register_hookspecs``.

A usage writer plugin can ship a thin reader: an ``aggregate_usage_source``
hookimpl that recognizes its own ``source_name`` and returns a ``UsageSnapshot``
(typically by calling one of the shared aggregation utils in
:mod:`imbue.mngr_usage.api`). The hook is ``firstresult`` -- the first plugin
that returns a non-None snapshot wins -- and a source no plugin claims falls
back to ``aggregate_process_cumulative`` in the dispatcher. This keeps
``mngr_usage`` itself source-agnostic: it dispatches and renders, never
hardcoding per-harness aggregation.
"""

import pluggy

from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot

hookspec = pluggy.HookspecMarker("mngr")


@hookspec(firstresult=True)
def aggregate_usage_source(
    source_name: str,
    # agent_id -> the agent's parsed usage events for this source
    agents_events: dict[str, list[UsageEvent]],
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate one source's events into a snapshot, or None to decline this source."""
