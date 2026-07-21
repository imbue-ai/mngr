"""Always-warm connection pool: cached agent resolution + an aggressive keepalive.

Resolving an agent runs mngr discovery on every call (~3s measured), which was
making every send / transcript-open / dialog-probe pay that cost afresh. Foreman
is long-lived and drives the same few agents repeatedly, so we cache per agent:

* the ``find_all_agents`` match list used by the send path, and
* the resolved ``(AgentInterface, OnlineHostInterface)`` used by everything else.

The resolved host object holds a *persistent* pyinfra/paramiko SSH connection that
subsequent commands reuse -- so once resolved, sends/reads hit a live socket. A
background maintainer keeps a warm connection open for *every* agent the registry
reports (the registry already filters to live coding agents), pinging each every
``_KEEPALIVE_INTERVAL_SECONDS`` and re-resolving on the spot if a ping fails (the
connection dropped, or the agent moved hosts). When an agent leaves the live set
its cached handle is dropped, so its connection is no longer kept warm. There is
no lazy first-use warming and nothing goes cold while idle: discovered -> warm,
gone -> dropped. That is what makes the *first* send and terminal-open as fast as
every later one.

The hot paths themselves never invalidate a handle on a transient error -- they
surface the error and reuse the cached connection; reconnection is the keepalive's
job alone, so a single failed command can't tear a warm connection down.

Cost: an idle persistent SSH connection is a kernel socket plus a periodic 1-byte
keepalive -- no compute, negligible memory.

Concurrency: the Flask server is threaded and a single paramiko connection is not
safe to drive from multiple threads at once. Providers cache one
``HostInterface`` per ``host_id``, so two agents on the same host resolve to the
*same* host object and must not drive it concurrently. ``run_on_host`` therefore
serializes on a lock keyed by the **host object** (not the per-agent handle):
resolution happens under the per-agent handle lock, but the command runs under
the shared per-host lock, so every command to a given connection is serialized
regardless of which agent triggered it. The keepalive fans out across hosts with
per-host worker threads and a bounded command timeout, so one hung host (TCP up
but unresponsive) can neither block the rest of the fleet nor stall the loop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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

# How often we ping each warm connection to keep it hot (and to notice a drop).
_KEEPALIVE_INTERVAL_SECONDS = 10.0
# Cached send matches self-heal within this window if an agent moved hosts.
_MATCHES_TTL_SECONDS = 60.0
# Bound every keepalive SSH touch so an unresponsive host (TCP up, no reply) can't
# block indefinitely; the fan-out keeps the rest of the fleet warm meanwhile.
_KEEPALIVE_TIMEOUT_SECONDS = 10.0
# Cap the keepalive fan-out so a large fleet doesn't spawn a thread per host.
_KEEPALIVE_MAX_WORKERS = 16


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
        # Serialization lock per *host object*, keyed by id() so two agents that
        # resolve to the same cached HostInterface share one lock around its
        # paramiko connection. Bounded by the number of distinct host objects ever
        # resolved (tiny); id() reuse after GC is harmless (only one live object
        # can hold a given id, and a GC'd host has no live callers to serialize).
        self._host_locks: dict[int, threading.Lock] = {}
        self._registry: Any = None
        self._stop = threading.Event()
        # Set by the registry when the known-agent set may have changed, so the
        # maintainer warms newly-"on" agents at once instead of after a full
        # keepalive interval (shrinks the user's cold-send window right after boot).
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle_for(self, agent_name: str) -> _Handle:
        with self._lock:
            handle = self._handles.get(agent_name)
            if handle is None:
                handle = _Handle()
                self._handles[agent_name] = handle
            return handle

    def _host_lock_for(self, host: OnlineHostInterface) -> threading.Lock:
        """Return the lock that serializes all commands to ``host``'s connection."""
        key = id(host)
        with self._lock:
            lock = self._host_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[key] = lock
            return lock

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

        Resolution happens under the per-agent handle lock; the command then runs
        under the per-*host* lock, so two agents sharing one host connection can't
        drive it concurrently. A transient failure is *not* invalidated here -- it
        propagates to the caller and the cached connection is left intact;
        reconnection is the keepalive's job (see ``_warm_one``).
        """
        handle = self._handle_for(agent_name)
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
            agent, host = handle.agent, handle.host
        # Execute outside the agent handle lock, under the shared per-host lock,
        # so all commands to this connection serialize even across agents.
        with self._host_lock_for(host):
            return fn(agent, host)

    # --- background keepalive --------------------------------------------

    def start_maintainer(self, registry: Any) -> None:
        """Start the always-warm keepalive thread, driven by the agent registry."""
        self._registry = registry
        # Warm the moment the live set changes (an agent appeared or left) rather
        # than waiting out the next keepalive interval.
        registry.set_on_change(self._wake.set)
        self._thread = threading.Thread(target=self._maintain, name="foreman-warm-pool", daemon=True)
        self._thread.start()

    def _maintain(self) -> None:
        # Tick once up front (registry may still be empty -- a no-op then), then on
        # every registry change and at least once per keepalive interval.
        self._safe_tick()
        while not self._stop.is_set():
            woken = self._wake.wait(_KEEPALIVE_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            if woken:
                self._wake.clear()
            self._safe_tick()

    def _safe_tick(self) -> None:
        try:
            self._tick()
        except Exception as e:  # noqa: BLE001 - a bad tick must not kill the maintainer
            logger.trace("warm-pool tick error: {}", e)

    def _warm_one(self, name: str) -> None:
        """Refresh one agent's send matches and ping its SSH connection to keep it hot.

        A failed ping means the connection dropped (or the agent moved hosts):
        forget the cached handle and re-resolve once, so it reconnects immediately
        on this same tick rather than at the next send.
        """
        try:
            self._touch(name)
        except Exception:  # noqa: BLE001 - reconnect on any keepalive failure
            self.invalidate(name)
            self._touch(name)

    def _touch(self, name: str) -> None:
        # Lazy import: terminal.py imports this module (ConnectionPool), so importing
        # it at module load would be circular. Mirrors send_via_pool's lazy import.
        from imbue.mngr_foreman.terminal import prewarm_agent_control_master

        # Send path (paramiko): keep matches fresh and the mngr connection hot.
        self.get_send_matches(name)
        self.run_on_host(name, _ping_host)
        # Terminal path (system-ssh): keep the ControlMaster socket hot too, so the
        # first terminal open is warm -- not just repeat opens.
        prewarm_agent_control_master(self, name)

    def _tick(self) -> None:
        if self._registry is None:
            return
        # The registry already filters to live coding agents, so every card is one
        # we keep warm.
        live_names = {card["name"] for card in self._registry.snapshot()}
        # Drop handles for agents that have left the live set (gone -> dropped).
        with self._lock:
            for name in list(self._handles):
                if name not in live_names:
                    self._handles.pop(name, None)
        if not live_names:
            return
        # Warm agents concurrently: each SSH touch is bounded by
        # _KEEPALIVE_TIMEOUT_SECONDS, and the fan-out means one hung host neither
        # blocks the others nor stalls the loop (worst case ~ one timeout, not N).
        futures = {}
        with ThreadPoolExecutor(
            max_workers=min(_KEEPALIVE_MAX_WORKERS, len(live_names)), thread_name_prefix="foreman-warm"
        ) as executor:
            for name in live_names:
                futures[name] = executor.submit(self._warm_one, name)
        for name, future in futures.items():
            error = future.exception()
            if error is not None:
                logger.trace("warm-pool keepalive for {} failed: {}", name, error)

    def stop(self) -> None:
        self._stop.set()
        # Unblock the maintainer's interval wait so it observes _stop at once.
        self._wake.set()


def _ping_host(_agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Cheap liveness touch to keep an SSH connection warm; no-op for local hosts.

    Bounded by a timeout so a host that accepts TCP but never replies can't wedge
    the keepalive on this connection.
    """
    if getattr(host, "is_local", False):
        return
    host.execute_stateful_command("true", timeout_seconds=_KEEPALIVE_TIMEOUT_SECONDS)


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
        # Foreman never resurrects a stopped agent: it only ever shows and targets
        # running agents, so a send must not auto-start anything.
        is_start_desired=False,
    )
    return list(result.failed_agents)
