"""Background thread pool for fire-and-forget ``stop_agent_on_host`` calls.

This module exists as a workaround for an mngr-core issue: ``Host.stop_agents``
can block on the kernel's TCP retransmit timeout (~15 minutes per call) when
the remote sandbox has gone away. Until the core call enforces its own
wall-clock budget, the mapreduce polling loop offloads each stop onto a
background thread so a single slow stop can't serialize the loop.

When mngr core is fixed, this whole module can be deleted and its call sites
in ``orchestration.py`` reverted to synchronous ``stop_agent_on_host`` calls.
"""

import time
from types import TracebackType

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.launching import stop_agent_on_host

_STOP_DRAIN_TIMEOUT_SECONDS = 60.0


class AgentStopper(MutableModel):
    """Fire-and-forget ``stop_agent_on_host`` calls so a slow stop can't wedge the polling loop.

    Production scenario: with a remote provider (e.g. Modal), an agent can
    publish its outputs archive to its volume and then have the underlying
    sandbox torn down before the polling loop notices. The post-finalize
    ``stop_agents`` SSH call then blocks on the kernel's TCP retransmit
    timeout -- observed at ~16 minutes per call -- which serializes the
    loop and starves it of observed finalizations. With 80 mappers under a
    4h GHA cap, the previous synchronous path left ~50 mappers unfinalized.

    Each ``submit`` spawns a daemon ``ObservableThread`` running
    ``stop_agent_on_host`` and returns immediately. The stopper context-exits
    with a bounded drain so a clean run still waits briefly for in-flight
    stops to finalize; any that haven't returned by then are abandoned (the
    provider reaps stale sandboxes via its own lifecycle independent of our
    cleanup).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    drain_timeout_seconds: float = Field(
        default=_STOP_DRAIN_TIMEOUT_SECONDS,
        description="How long ``__exit__`` waits for in-flight stops before giving up.",
    )
    threads: list[ObservableThread] = Field(
        default_factory=list,
        description="Threads spawned by ``submit``, joined on ``__exit__``.",
    )

    def submit(self, host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
        thread = ObservableThread(
            target=stop_agent_on_host,
            args=(host, agent_id, agent_name),
            name=f"stop-{agent_name}",
            # Anything that escapes ``stop_agent_on_host``'s own ``MngrError``
            # catch (e.g. an unwrapped ``OSError`` from paramiko) is logged via
            # ObservableThread's error logger, but we don't want it to crash the drain
            # ``join()`` -- the stops are best-effort cleanup.
            suppressed_exceptions=(BaseException,),
        )
        thread.start()
        self.threads.append(thread)

    def __enter__(self) -> "AgentStopper":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        in_flight = [t for t in self.threads if t.is_alive()]
        if not in_flight:
            return
        logger.info(
            "Waiting up to {}s for {} in-flight agent stop(s) to finish",
            self.drain_timeout_seconds,
            len(in_flight),
        )
        deadline = time.monotonic() + self.drain_timeout_seconds
        for thread in in_flight:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        leaked = [t for t in self.threads if t.is_alive()]
        if leaked:
            logger.warning(
                "Abandoned {} agent stop thread(s) after {}s drain timeout",
                len(leaked),
                self.drain_timeout_seconds,
            )
