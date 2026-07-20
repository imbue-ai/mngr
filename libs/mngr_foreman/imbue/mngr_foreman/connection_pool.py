"""Warm connection pool: cached agent resolution + a 24/7 keepalive.

Resolving an agent runs mngr discovery on every call (~3s measured), which was
making every send / transcript-open / dialog-probe pay that cost afresh. Foreman
is long-lived and drives the same few agents repeatedly, so we cache per agent:

* the ``find_all_agents`` match list used by the send path, and
* the resolved ``(AgentInterface, OnlineHostInterface)`` used by everything else.

The resolved host object holds a *persistent* pyinfra/paramiko SSH connection that
subsequent commands reuse and that mngr reconnects lazily if it drops -- so once
resolved, sends/reads hit a live socket. A background maintainer keeps those
connections warm for every agent the registry reports in an "on" (RUNNING /
WAITING) state, skipping STOPPED/DONE/UNKNOWN (never wakes anything) and
local-provider hosts (no SSH at all). Registry membership drives add/remove.

Cost: an idle persistent SSH connection is a kernel socket plus a periodic 1-byte
keepalive -- no compute, negligible memory. This trades ~nothing for removing the
~3s discovery from the hot paths.

Concurrency: the Flask server is threaded and a single paramiko connection is not
safe to drive from multiple threads at once, so each host handle carries a lock
and all access to a host is serialized through ``run_on_host``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import TypeVar

from loguru import logger

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ErrorBehavior

T = TypeVar("T")

# States whose hosts we keep warm. Everything else is left alone (never woken).
_ON_STATES = frozenset({"RUNNING", "WAITING", "RUNNING_UNKNOWN_AGENT_TYPE"})
_KEEPALIVE_INTERVAL_SECONDS = 25.0
# Cached send matches self-heal within this window if an agent moved hosts.
_MATCHES_TTL_SECONDS = 60.0


@dataclass
class _Handle:
    """Per-agent cache slot; each field is resolved lazily and reused."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    matches: list[Any] | None = None
    matches_at: float = 0.0
    agent: AgentInterface | None = None
    host: OnlineHostInterface | None = None


class ConnectionPool:
    """Thread-safe cache of resolved agent handles, with a background keepalive."""

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self.mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        self._handles: dict[str, _Handle] = {}
        self._registry: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle_for(self, agent_name: str) -> _Handle:
        with self._lock:
            handle = self._handles.get(agent_name)
            if handle is None:
                handle = _Handle()
                self._handles[agent_name] = handle
            return handle

    def invalidate(self, agent_name: str) -> None:
        """Forget a cached handle so the next access re-resolves (e.g. after error)."""
        with self._lock:
            self._handles.pop(agent_name, None)

    # --- send-path resolution (cached find_all_agents matches) -----------

    def get_send_matches(self, agent_name: str) -> list[Any]:
        """Return cached ``find_all_agents`` matches for ``agent_name`` (resolve once)."""
        handle = self._handle_for(agent_name)
        with handle.lock:
            fresh = handle.matches is not None and (time.monotonic() - handle.matches_at) < _MATCHES_TTL_SECONDS
            if not fresh:
                address = parse_agent_address(agent_name)
                handle.matches = list(
                    find_all_agents(addresses=[address], filter_all=False, target_state=None, mngr_ctx=self.mngr_ctx)
                )
                handle.matches_at = time.monotonic()
            return handle.matches

    # --- host-path resolution (cached agent + host) ----------------------

    def run_on_host(self, agent_name: str, fn: Callable[[AgentInterface, OnlineHostInterface], T]) -> T:
        """Resolve (cached) and run ``fn(agent, host)`` serialized on the host lock.

        Drops the cached handle on any failure so the next call re-resolves.
        """
        handle = self._handle_for(agent_name)
        try:
            with handle.lock:
                if handle.agent is None or handle.host is None:
                    address = parse_agent_address(agent_name)
                    host_ref, agent_ref = find_one_agent(address, self.mngr_ctx)
                    handle.agent, handle.host = resolve_to_started_host_and_agent(
                        host_ref=host_ref,
                        agent_ref=agent_ref,
                        allow_auto_start=False,
                        mngr_ctx=self.mngr_ctx,
                    )
                return fn(handle.agent, handle.host)
        except Exception:
            self.invalidate(agent_name)
            raise

    # --- background keepalive --------------------------------------------

    def start_maintainer(self, registry: Any) -> None:
        """Start the 24/7 warm-pool thread, driven by the agent registry."""
        self._registry = registry
        self._thread = threading.Thread(target=self._maintain, name="foreman-warm-pool", daemon=True)
        self._thread.start()

    def _maintain(self) -> None:
        while not self._stop.wait(_KEEPALIVE_INTERVAL_SECONDS):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 - a bad tick must not kill the maintainer
                logger.trace("warm-pool tick error: {}", e)

    def _tick(self) -> None:
        if self._registry is None:
            return
        on_agents = {
            card["name"] for card in self._registry.snapshot() if str(card.get("state", "")).upper() in _ON_STATES
        }
        # Drop handles for agents that are gone or no longer "on".
        with self._lock:
            for name in list(self._handles):
                if name not in on_agents:
                    self._handles.pop(name, None)
        # Warm each on-state agent: keep the send match-list fresh AND the SSH
        # connection alive, so even the first user action hits a warm path.
        for name in on_agents:
            try:
                self.get_send_matches(name)
                self.run_on_host(name, _ping_host)
            except Exception as e:  # noqa: BLE001 - one unreachable host must not stop the rest
                logger.trace("warm-pool keepalive for {} failed: {}", name, e)

    def stop(self) -> None:
        self._stop.set()


def _ping_host(_agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Cheap liveness touch to keep an SSH connection warm; no-op for local hosts."""
    if getattr(host, "is_local", False):
        return
    host.execute_stateful_command("true")


def send_via_pool(pool: ConnectionPool, agent_name: str, message: str) -> list[tuple[str, str]]:
    """Send ``message`` using the pool's cached matches. Returns failed (name, error) pairs.

    Imported lazily by the messaging module to avoid a circular import.
    """
    from imbue.mngr.api.message import send_message_to_agents

    matches = pool.get_send_matches(agent_name)
    if not matches:
        pool.invalidate(agent_name)
        raise LookupError(f"No agent found matching {agent_name!r}")
    result = send_message_to_agents(
        mngr_ctx=pool.mngr_ctx,
        message_content=message,
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=True,
    )
    return list(result.failed_agents)
