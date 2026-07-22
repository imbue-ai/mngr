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
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from imbue.mngr.utils.thread_cleanup import cleanup_thread_local_resources

T = TypeVar("T")


class HostBusyError(Exception):
    """Raised when a host's lock can't be acquired within the allotted time.

    A hung/slow connection holds its per-host (or per-agent) lock while an SSH
    command runs. WITHOUT a bound, every subsequent request for that host -- the 1s
    input-state poll, transcript ticks, sends -- would block on the lock in its own
    werkzeug thread FOREVER, so threads pile up unboundedly until the GIL is
    contended and the whole server degrades (the "everything goes offline" cascade).
    Bounding the acquire makes a waiter give up fast and the caller degrade (skip the
    poll / retry next tick) instead of wedging a thread. This is the load-bearing
    robustness guarantee: no single bad connection can exhaust the thread pool.
    """

# How often we ping each warm connection to keep it hot (and to notice a drop).
_KEEPALIVE_INTERVAL_SECONDS = 10.0
# Cached send matches self-heal within this window if an agent moved hosts.
_MATCHES_TTL_SECONDS = 60.0
# Bound every keepalive SSH touch so an unresponsive host (TCP up, no reply) can't
# block indefinitely; the fan-out keeps the rest of the fleet warm meanwhile.
_KEEPALIVE_TIMEOUT_SECONDS = 10.0
# Cap the keepalive fan-out so a large fleet doesn't spawn a thread per host.
_KEEPALIVE_MAX_WORKERS = 16
# SSH keepalive interval: paramiko pings this often so a SILENTLY-dropped peer (a
# tunnel gone with no FIN/RST) is detected within ~2 intervals and its transport dies,
# instead of the reader thread blocking in read_all forever (see _disconnect_host).
_SSH_KEEPALIVE_SECONDS = 15
# Default host/handle lock-acquire bound (see HostBusyError for why bounding matters).
# Generous enough that a normal serialized command (~10s timeout) never false-trips;
# high-frequency callers pass a much shorter timeout to skip a busy tick.
_DEFAULT_LOCK_TIMEOUT_SECONDS = 20.0


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
        # One persistent keepalive fan-out executor for the pool's whole life.
        # Re-creating a ThreadPoolExecutor per tick spawned (and reaped) worker
        # threads every interval; each such thread that touched pyinfra/gevent left
        # its thread-local gevent Hub -- and that Hub's OS-level wakeup pipe (an
        # epoll+eventfd pair) -- to leak on exit. A single long-lived executor keeps
        # a bounded set of reused workers, and each task destroys its thread's Hub
        # when it finishes (see _warm_one), so nothing accumulates.
        self._executor: ThreadPoolExecutor | None = None

    def _handle_for(self, agent_name: str) -> _Handle:
        with self._lock:
            return self._handles.setdefault(agent_name, _Handle())

    def _get_executor(self) -> ThreadPoolExecutor:
        """Return the pool's persistent keepalive executor, creating it on first use.

        Lazily built (rather than in ``__init__``) so a pool that never warms --
        e.g. a unit test that only exercises resolution -- spawns no threads.
        """
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=_KEEPALIVE_MAX_WORKERS, thread_name_prefix="foreman-warm"
                )
            return self._executor

    def _host_lock_for(self, host: OnlineHostInterface) -> threading.Lock:
        """Return the lock that serializes all commands to ``host``'s connection."""
        with self._lock:
            return self._host_locks.setdefault(id(host), threading.Lock())

    def _disconnect_host(self, host: OnlineHostInterface) -> None:
        """Best-effort close of a host's SSH connection so its paramiko reader thread
        exits. A dropped/half-open connection whose client is never closed leaves that
        daemon thread blocked in ``read_all`` forever -- the foreman thread leak. GC
        cannot reap it (the live thread, plus the provider's cached host ref, keep the
        object alive), so we must close explicitly whenever we stop using a connection.
        """
        try:
            host.disconnect()
        except Exception as e:  # noqa: BLE001 - a failed close must not break the caller
            logger.trace("pool: host disconnect failed (ignored): {}", e)

    def _drop_handles(self, names: set[str]) -> None:
        """Remove the named handles and disconnect (see _disconnect_host) any host no
        surviving handle still references -- never one another agent shares. The
        disconnect (network I/O) runs OUTSIDE the pool lock.
        """
        to_close: list[OnlineHostInterface] = []
        with self._lock:
            dropped = [self._handles.pop(n) for n in names if n in self._handles]
            surviving = {id(h.host) for h in self._handles.values() if h.host is not None}
            for h in dropped:
                if h.host is not None and id(h.host) not in surviving:
                    to_close.append(h.host)
                    # Prune this host's now-dead lock too, else _host_locks grows one
                    # permanent entry per re-resolved host object (a slow unbounded leak).
                    self._host_locks.pop(id(h.host), None)
        for host in to_close:
            self._disconnect_host(host)

    def _enable_keepalive(self, host: OnlineHostInterface) -> None:
        """Turn on SSH keepalive so a silently-dropped peer is detected and its reader
        thread exits on its own (proactive backstop to _drop_handles' explicit close)."""
        try:
            host._get_paramiko_transport().set_keepalive(_SSH_KEEPALIVE_SECONDS)  # ty: ignore[unresolved-attribute]
        except Exception as e:  # noqa: BLE001 - local agents have no transport; best effort
            logger.trace("pool: set_keepalive failed (ignored): {}", e)

    def invalidate(self, agent_name: str) -> None:
        """Forget a cached handle (and close its now-unused connection) so the next
        access re-resolves -- e.g. after a keepalive ping failed on a dropped host."""
        self._drop_handles({agent_name})

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

    @staticmethod
    @contextmanager
    def _bounded(lock: threading.Lock, timeout: float, what: str) -> Iterator[None]:
        """Acquire ``lock`` within ``timeout`` or raise HostBusyError (see there)."""
        if not lock.acquire(timeout=timeout):
            raise HostBusyError(what)
        try:
            yield
        finally:
            lock.release()

    def run_on_host(
        self,
        agent_name: str,
        fn: Callable[[AgentInterface, OnlineHostInterface], T],
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> T:
        """Resolve (cached) and run ``fn(agent, host)`` serialized on the host lock.

        Resolution runs under the per-agent handle lock; the command then runs under
        the per-*host* lock, so two agents sharing one connection can't drive it at
        once. BOTH acquisitions are bounded by ``lock_timeout`` (raising HostBusyError
        rather than blocking forever) -- note the effective worst-case wait is ~2x
        ``lock_timeout`` since it applies to each. A transient command failure is *not*
        invalidated here -- it propagates and the cached connection is left intact;
        reconnection is the keepalive's job (see ``_warm_one``).

        Ceiling: the *first* resolution (find_one_agent / resolve) runs unbounded inside
        the handle lock, so a hung discovery still parks this one thread -- but waiters
        now bail with HostBusyError instead of piling up behind it.
        """
        handle = self._handle_for(agent_name)
        with self._bounded(handle.lock, lock_timeout, f"resolve {agent_name}"):
            if handle.agent is None or handle.host is None:
                address = parse_agent_address(agent_name)
                host_ref, agent_ref = find_one_agent(address, self.mngr_ctx)
                handle.agent, handle.host = resolve_to_started_host_and_agent(
                    host_ref=host_ref,
                    agent_ref=agent_ref,
                    allow_auto_start=False,
                    mngr_ctx=self.mngr_ctx,
                )
                # Fresh connection -> arm SSH keepalive so a silent peer-drop is noticed.
                self._enable_keepalive(handle.host)
            agent, host = handle.agent, handle.host
        # Execute outside the agent handle lock, under the shared per-host lock,
        # so all commands to this connection serialize even across agents.
        with self._bounded(self._host_lock_for(host), lock_timeout, f"host {agent_name}"):
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
            try:
                self._touch(name)
            except Exception:  # noqa: BLE001 - reconnect on any keepalive failure
                self.invalidate(name)
                self._touch(name)
        finally:
            # This task ran pyinfra/paramiko work, which spins up a thread-local
            # gevent Hub with an OS-level wakeup pipe. Destroy it now so the pooled
            # worker thread carries nothing over between ticks -- otherwise those
            # epoll+eventfd fds accumulate for the life of the process. No-op on a
            # thread that never touched gevent. (mngr/utils/thread_cleanup.py)
            cleanup_thread_local_resources()

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
        # Drop + close handles for agents that left the live set (gone -> dropped),
        # reaping their connection's reader thread (see _drop_handles / _disconnect_host).
        with self._lock:
            gone = {name for name in self._handles if name not in live_names}
        if gone:
            self._drop_handles(gone)
        if not live_names:
            return
        # Warm agents concurrently on the persistent executor: each SSH touch is
        # bounded by _KEEPALIVE_TIMEOUT_SECONDS, and the fan-out means one hung host
        # neither blocks the others nor stalls the loop (worst case ~ one timeout,
        # not N). We submit and then wait on every future so the tick still barriers
        # on the whole fleet, exactly as the per-tick executor's `with` block did.
        executor = self._get_executor()
        futures = {name: executor.submit(self._warm_one, name) for name in live_names}
        for name, future in futures.items():
            error = future.exception()
            if error is not None:
                logger.trace("warm-pool keepalive for {} failed: {}", name, error)

    def stop(self) -> None:
        self._stop.set()
        # Unblock the maintainer's interval wait so it observes _stop at once.
        self._wake.set()
        # Close every warm connection so its paramiko reader thread exits.
        with self._lock:
            hosts = [h.host for h in self._handles.values() if h.host is not None]
            self._handles = {}
            executor, self._executor = self._executor, None
        for host in hosts:
            self._disconnect_host(host)
        # Tear down the keepalive workers (and let their gevent Hubs go with them).
        if executor is not None:
            executor.shutdown(wait=False)


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

    The send runs under the per-host lock (via ``run_on_host``) so it serializes with
    the pool's other work on that host (transcript reads, pane probes, interrupts)
    rather than driving one connection from two threads at once. Caveat by provider:
    the docker provider caches its host object by id, so ``send_message_to_agents``
    reuses the very connection this lock guards; the ssh/local providers mint a fresh
    host per ``get_host``, so there the send opens its own connection -- the lock still
    serializes foreman's threads but that send doesn't reuse the warm connection.
    Threading the pool's resolved host into ``send_message_to_agents`` (to always reuse
    the warm one) is a cross-package change, deferred until send-path cost shows up.
    """
    from imbue.mngr.api.message import send_message_to_agents

    matches = pool.get_send_matches(agent_name)
    if not matches:
        pool.invalidate(agent_name)
        raise LookupError(f"No agent found matching {agent_name!r}")

    def _send(_agent: AgentInterface, _host: OnlineHostInterface) -> list[tuple[str, str]]:
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

    return pool.run_on_host(agent_name, _send)
