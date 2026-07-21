"""Live registry of running coding agents, kept fresh by a discovery poll loop.

Every ``POLL_INTERVAL_SECONDS`` a background thread calls ``list_agents``
in-process -- the same discovery ``mngr list`` runs, no subprocess -- keeps only
the *live coding* agents (a coding-harness type in a RUNNING or WAITING state),
and publishes that set to the SSE subscribers and the warm pool. Dead, stopped,
done, and non-coding agents never appear.

There are no per-agent deltas: each poll whose projected result changed
broadcasts one full snapshot, which the browser re-renders. The loop is
sequential (list, then wait the interval, then list again), so a slow list just
delays the next pass and never overlaps itself.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from collections.abc import Iterator
from typing import Final

from loguru import logger

from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_foreman.harness import transcript_strategy_for

# How often the discovery loop re-lists agents (the interval *between* passes).
POLL_INTERVAL_SECONDS: Final[float] = 3.0

# The coding-harness agent types foreman shows -- the transcript-driven agents a
# user actually chats with. Every other type (mngr system/worker types) is hidden.
CODING_AGENT_TYPES: Final[frozenset[str]] = frozenset({"claude", "codex", "opencode", "pi-coding"})

# Only actively-running agents are shown. Every other lifecycle state
# (STOPPED / DONE / REPLACED / UNKNOWN / RUNNING_UNKNOWN_AGENT_TYPE and any
# tombstone) is hidden -- you cannot open, warm, or message a dead agent.
LIVE_STATES: Final[frozenset[str]] = frozenset({"RUNNING", "WAITING"})

# Bound each subscriber queue so a dead/slow SSE client cannot grow memory
# without limit; on overflow we drop the client (it reconnects and re-seeds).
_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 256


def _state_of(agent: AgentDetails) -> str:
    return str(agent.state.value if hasattr(agent.state, "value") else agent.state).upper()


def _is_live_coding(agent: AgentDetails) -> bool:
    """True for a coding-harness agent that is currently running or waiting."""
    return agent.type in CODING_AGENT_TYPES and _state_of(agent) in LIVE_STATES


class AgentRegistry:
    """Thread-safe set of live coding agents, refreshed by a discovery poll loop."""

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self._mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        self._agents: dict[str, AgentDetails] = {}
        self._subscribers: set[queue.Queue[dict]] = set()
        # Fired whenever the live-agent *name set* changes, so the warm pool warms
        # newly-appeared agents and drops departed ones without waiting out its own
        # keepalive interval.
        self._on_change: Callable[[], None] | None = None
        self._live_names: frozenset[str] = frozenset()
        self._last_broadcast: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the background discovery poll loop (idempotent, does not block)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._poll_loop, name="foreman-registry-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the live-agent name set changes."""
        self._on_change = callback

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _poll_once(self) -> None:
        """List agents once, keep the live coding ones, and publish the new set."""
        try:
            result = list_agents(
                self._mngr_ctx,
                is_streaming=False,
                error_behavior=ErrorBehavior.CONTINUE,
                reset_caches=True,
            )
        except Exception as e:  # noqa: BLE001 - a bad provider must not kill the poll loop
            logger.debug("Agent discovery poll failed (keeping last set): {}", e)
            return
        live = {str(a.id): a for a in result.agents if _is_live_coding(a)}
        with self._lock:
            self._agents = live
        self._publish(live)

    def _publish(self, live: dict[str, AgentDetails]) -> None:
        """Broadcast a fresh snapshot if it changed, and wake the pool if membership did."""
        cards = self.snapshot()
        payload = json.dumps(cards, sort_keys=True)
        if payload != self._last_broadcast:
            self._last_broadcast = payload
            self._broadcast({"type": "snapshot", "agents": cards})
        names = frozenset(str(a.name) for a in live.values())
        if names != self._live_names:
            self._live_names = names
            if self._on_change is not None:
                self._on_change()

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
        """Yield an initial snapshot then live snapshots until the client leaves."""
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
        "state": _state_of(agent),
        "host_name": agent.host.name,
        "provider": str(agent.host.provider_name),
        "labels": dict(agent.labels),
        "activity_time": activity.isoformat() if activity else None,
        # Chat (live transcript + composer) is available for any type foreman has a
        # transcript strategy for (claude, codex, opencode, pi-coding); others are
        # terminal-only.
        "supports_chat": transcript_strategy_for(agent.type) is not None,
    }
