"""Closing SSH transports that a machine suspension left half-open.

A laptop that sleeps mid-command wakes holding a socket connected to nothing,
and nothing blocked on it finds out promptly:

- pyinfra's per-command ``_timeout`` never fires in mngr's usage: mngr does not
  monkeypatch gevent, so the paramiko reads it waits over block the hub the
  timer runs on.
- The paramiko keepalive writes but never waits for a reply. Where the peer's
  reset can reach us the write surfaces the death within a keepalive interval;
  through a NAT whose mapping the sleep killed nothing comes back at all.
- What is left is the kernel's retransmission timeout: ~48s after the wake
  (measured on macOS 26), and never for a socket with nothing in flight.

This watchdog closes the transport from outside instead, which wakes every
blocked channel on it; mngr's transient-SSH retry then reconnects. Registered at
the ``_ensure_connected`` chokepoint, so it covers every host connection mngr
runs commands over. The sshd-readiness probes in ``providers/ssh_utils.py``
carry their own socket timeouts and are left alone.
"""

import threading
import weakref
from collections.abc import Callable
from typing import Final

from loguru import logger
from paramiko import SSHException
from paramiko import Transport
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.suspension import ClockReading
from imbue.imbue_common.suspension import read_clocks
from imbue.imbue_common.suspension import was_suspended_since

# Small enough that a blocked command is released within seconds of the lid
# opening (against ~48s-to-never otherwise); large enough that watching is free.
_DEFAULT_CHECK_INTERVAL_SECONDS: Final[float] = 5.0


class _WatchedTransport(FrozenModel):
    """One registered transport and when it was established."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    transport_ref: weakref.ref[Transport] = Field(description="Weak reference, so watching never keeps one alive")
    established_at: ClockReading = Field(description="Both clocks, sampled when the transport was built")


class SuspensionWatchdog(MutableModel):
    """Watches registered SSH transports and closes the ones a suspension outlived.

    Held by :class:`MngrContext`, since "the machine stopped" is true of every
    connection at once. The background thread starts on the first registration
    and runs until :meth:`shutdown` or process exit; nothing in production calls
    ``shutdown``, which costs one thread per context for consumers that build a
    context per call. Closed or collected transports are dropped on the next
    pass, so long-running consumers do not accumulate them.
    """

    check_interval_seconds: float = Field(
        default=_DEFAULT_CHECK_INTERVAL_SECONDS,
        description="Seconds between clock comparisons.",
    )
    clock_fn: Callable[[], ClockReading] = Field(
        default=read_clocks,
        description="Injectable pair-of-clocks sampler, overridden in tests to pose a suspension.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _watched: list[_WatchedTransport] = PrivateAttr(default_factory=list)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _shutdown_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def register(self, transport: Transport | None) -> None:
        """Start watching ``transport``, stamping it with both clocks. ``None`` (a local connector) is a no-op."""
        if transport is None:
            return
        with self._lock:
            self._watched.append(
                _WatchedTransport(transport_ref=weakref.ref(transport), established_at=self.clock_fn())
            )
        self._ensure_thread_started()

    def close_transports_that_outlived_a_suspension(self) -> int:
        """Close every watched transport built before a suspension; return how many.

        Also drops transports that were collected or closed on their own. Closing
        happens outside the lock, since closing a wedged transport can block.
        """
        now = self.clock_fn()
        doomed: list[Transport] = []
        with self._lock:
            surviving: list[_WatchedTransport] = []
            for watched in self._watched:
                transport = watched.transport_ref()
                if transport is None or not transport.is_active():
                    continue
                if was_suspended_since(watched.established_at, now=now):
                    doomed.append(transport)
                    continue
                surviving.append(watched)
            self._watched = surviving
        for transport in doomed:
            logger.info(
                "Closing the SSH transport to {}: it was established before this machine suspended, so anything "
                "waiting on it is waiting on a connection whose peer is already gone",
                _describe_peer(transport),
            )
            try:
                transport.close()
            except (OSError, SSHException) as e:
                logger.debug("Error closing a suspension-stale SSH transport: {}", e)
        return len(doomed)

    def shutdown(self) -> None:
        """Stop the background thread and forget every watched transport. Idempotent."""
        self._shutdown_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
            self._watched = []
        if thread is not None:
            thread.join(timeout=self.check_interval_seconds + 1.0)

    def _ensure_thread_started(self) -> None:
        with self._lock:
            if self._thread is not None or self._shutdown_event.is_set():
                return
            thread = threading.Thread(target=self._run, name="ssh-suspension-watchdog", daemon=True)
            # Started before the slot is filled so shutdown never joins an unstarted thread.
            thread.start()
            self._thread = thread

    def _run(self) -> None:
        # Unsupervised thread: paramiko's socket errors must not retire it for
        # the life of the process. Anything else is a bug and should propagate.
        while not self._shutdown_event.wait(timeout=self.check_interval_seconds):
            try:
                self.close_transports_that_outlived_a_suspension()
            except (OSError, SSHException) as e:
                logger.warning("Suspension watchdog pass failed; still watching: {}", e)


def _describe_peer(transport: Transport) -> str:
    """``host:port`` of the transport's peer, or a placeholder once the socket is gone."""
    try:
        peer = transport.getpeername()
    except (OSError, SSHException, AttributeError):
        return "an unknown peer"
    return f"{peer[0]}:{peer[1]}"
