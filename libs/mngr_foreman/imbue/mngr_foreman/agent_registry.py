"""Live registry of mngr agents: fast in-process seed + follow ``mngr observe``.

The registry holds the current ``dict[agent_id, AgentDetails]`` and fans changes
out to per-connection subscriber queues (the SSE ``/api/agents/stream`` endpoint
drains one). It is kept live by an ``mngr observe --stream-events`` subprocess
whose agents-stream lines we parse with ``parse_observe_event_line`` (AGENT_STATE
/ AGENTS_FULL_STATE / AGENT_REMOVED). The subprocess is run on the shared
concurrency group, mirroring mngr's own observe-consumer pattern.

Crucially the initial ``list_agents`` seed runs on a *background thread*, not on
the startup critical path: it used to run before the Flask port bound and could
take 20-30s against slow/dead hosts. The port now binds immediately; the seed
fills the map a few seconds later. We seed in-process (rather than just waiting
for observe's own first snapshot) because observe is a *separate* ``mngr``
subprocess that pays the same multi-second CLI cold-start before it can emit --
the in-process seed populates the list far sooner. The seed merges without
clobbering any observe entry that already landed (``setdefault``), and observe
remains the source of truth for all live updates thereafter.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from collections.abc import Iterator
from typing import Final

from loguru import logger

from imbue.mngr.api.list import list_agents
from imbue.mngr.api.observe import AgentRemovedEvent
from imbue.mngr.api.observe import AgentStateEvent
from imbue.mngr.api.observe import FullAgentStateEvent
from imbue.mngr.api.observe import parse_observe_event_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary

# Bound each subscriber queue so a dead/slow SSE client cannot grow memory
# without limit; on overflow we drop the client (it reconnects and re-seeds).
_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 256


class AgentRegistry:
    """Thread-safe live map of agent_id -> AgentDetails with change fan-out.

    Tracks every agent in mngr's view -- foreman has no label filter.
    """

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self._mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        self._agents: dict[str, AgentDetails] = {}
        self._subscribers: set[queue.Queue[dict]] = set()
        # Fired whenever the known-agent set may have changed (full-state snapshot
        # or per-agent upsert) so the warm pool can warm new "on" agents promptly
        # instead of waiting out its keepalive interval.
        self._on_agents_changed: Callable[[], None] | None = None
        self._started = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start observe (live) and kick the seed on a background thread (idempotent).

        Neither call blocks: ``app.run()`` binds the port immediately after this
        returns. The seed used to run here synchronously and could take 20-30s
        against slow/dead hosts, delaying the bind that whole time.
        """
        if self._started:
            return
        self._started = True
        self._start_observe_stream()
        self._start_background_seed()

    def _start_background_seed(self) -> None:
        thread = threading.Thread(target=self._seed_snapshot, name="foreman-registry-seed", daemon=True)
        thread.start()

    def _seed_snapshot(self) -> None:
        """One-shot in-process ``list_agents`` to fill the map before observe emits.

        Runs off the critical path (background thread). Merges with ``setdefault``
        so any observe entry that already arrived wins; a bad provider degrades to
        an empty seed rather than sinking the server.
        """
        try:
            result = list_agents(
                self._mngr_ctx,
                is_streaming=False,
                error_behavior=ErrorBehavior.CONTINUE,
                reset_caches=True,
            )
        except Exception as e:  # noqa: BLE001 - a bad provider must not sink the seed thread
            logger.debug("Background agent seed failed (observe will populate): {}", e)
            return
        added = 0
        with self._lock:
            for agent in result.agents:
                if str(agent.id) not in self._agents:
                    self._agents[str(agent.id)] = agent
                    added += 1
        if added:
            logger.info("Background-seeded {} agent(s) before observe caught up", added)
            self._broadcast({"type": "snapshot", "agents": self.snapshot()})
            self._notify_agents_changed()

    def set_on_agents_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the known-agent set may have changed."""
        self._on_agents_changed = callback

    def _notify_agents_changed(self) -> None:
        callback = self._on_agents_changed
        if callback is not None:
            callback()

    def _start_observe_stream(self) -> None:
        self._mngr_ctx.concurrency_group.run_process_in_background(
            command=[resolve_mngr_binary(), "observe", "--stream-events", "--quiet"],
            on_output=self._on_observe_line,
            is_checked_by_group=False,
        )

    # --- observe stream --------------------------------------------------

    def _on_observe_line(self, line: str, is_stdout: bool) -> None:
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = parse_observe_event_line(stripped)
        except Exception as e:  # noqa: BLE001 - never let one bad line kill the reader thread
            logger.trace("Skipping unparseable observe line: {}", e)
            return
        if event is None:
            return

        if isinstance(event, FullAgentStateEvent):
            self._apply_full_state(event.agents)
        elif isinstance(event, AgentStateEvent):
            self._apply_upsert(event.agent)
        elif isinstance(event, AgentRemovedEvent):
            self._apply_remove(str(event.agent_id))

    def _apply_full_state(self, agents: tuple[AgentDetails, ...]) -> None:
        with self._lock:
            self._agents = {str(a.id): a for a in agents}
        self._broadcast({"type": "snapshot", "agents": self.snapshot()})
        self._notify_agents_changed()

    def _apply_upsert(self, agent: AgentDetails) -> None:
        with self._lock:
            self._agents[str(agent.id)] = agent
        self._broadcast({"type": "upsert", "agent": _agent_to_card(agent)})
        self._notify_agents_changed()

    def _apply_remove(self, agent_id: str) -> None:
        with self._lock:
            existed = self._agents.pop(agent_id, None) is not None
        if existed:
            self._broadcast({"type": "remove", "agent_id": agent_id})

    # --- read + subscribe ------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            agents = list(self._agents.values())
        return [_agent_to_card(a) for a in sorted(agents, key=_sort_key)]

    def get_agent(self, name_or_id: str) -> AgentDetails | None:
        with self._lock:
            for agent in self._agents.values():
                if str(agent.name) == name_or_id or str(agent.id) == name_or_id:
                    return agent
        return None

    def subscribe(self) -> Iterator[dict]:
        """Yield an initial snapshot then live deltas until the client leaves."""
        q: queue.Queue[dict] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.add(q)
        try:
            yield {"type": "snapshot", "agents": self.snapshot()}
            while True:
                yield q.get()
        finally:
            with self._lock:
                self._subscribers.discard(q)

    def _broadcast(self, message: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except queue.Full:
                logger.debug("Dropping a slow foreman list-stream subscriber (queue full)")
                with self._lock:
                    self._subscribers.discard(q)


def _sort_key(agent: AgentDetails) -> tuple:
    # Most-recently-active first; agents with no activity sort last.
    activity = agent.agent_activity_time or agent.user_activity_time or agent.create_time
    return (0 if activity else 1, -(activity.timestamp() if activity else 0.0), str(agent.name))


def _agent_to_card(agent: AgentDetails) -> dict:
    """Project an ``AgentDetails`` down to the fields the list UI needs."""
    activity = agent.agent_activity_time or agent.user_activity_time
    return {
        "id": str(agent.id),
        "name": str(agent.name),
        "type": agent.type,
        "state": str(agent.state.value if hasattr(agent.state, "value") else agent.state),
        "host_name": agent.host.name,
        "provider": str(agent.host.provider_name),
        "labels": dict(agent.labels),
        "activity_time": activity.isoformat() if activity else None,
        "is_claude": agent.type == "claude",
    }
