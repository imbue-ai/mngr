import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import wait
from datetime import datetime
from datetime import timezone

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import tail_discovery_events_from_offset
from imbue.mngr.api.discovery_events import write_provider_discovery_snapshot
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderError
from imbue.mngr.interfaces.data_types import BoundedProviderDiscoveryResult
from imbue.mngr.interfaces.provider_instance import HostDiscoveryReadRegistry
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.thread_cleanup import mngr_executor


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_provider_config(provider: BaseProviderInstance, mngr_ctx: MngrContext) -> ProviderInstanceConfig:
    """Return the configured block for a provider, or a default block for implicit-default instances.

    Mirrors the default-config fallback used by the listing path: an implicit-default
    provider (no explicit ``[providers.<name>]`` block) uses its name as the backend.
    """
    explicit = mngr_ctx.config.providers.get(provider.name)
    if explicit is not None:
        return explicit
    return ProviderInstanceConfig(backend=ProviderBackendName(str(provider.name)))


def _discover_one_provider(
    provider: BaseProviderInstance,
    mngr_ctx: MngrContext,
    host_discovery_timeout_seconds: float,
    agent_discovery_timeout_seconds: float,
    include_destroyed: bool,
    registry: HostDiscoveryReadRegistry,
) -> BoundedProviderDiscoveryResult:
    """Run a single provider's per-host-bounded discovery. Raises on failure.

    A slow/wedged host is marked UNKNOWN within the returned result rather than
    stalling the whole provider's snapshot. ``registry`` carries in-flight per-host
    reads across polls so a wedged host is not re-read on every poll.
    """
    return provider.discover_hosts_and_agents_within_timeouts(
        cg=mngr_ctx.concurrency_group,
        host_discovery_timeout_seconds=host_discovery_timeout_seconds,
        agent_discovery_timeout_seconds=agent_discovery_timeout_seconds,
        include_destroyed=include_destroyed,
        registry=registry,
    )


class _ProviderDiscoveryPoller(MutableModel):
    """Polls one provider's discovery on its own cadence and writes per-provider snapshots.

    Each provider gets an independent poller (and thread), so a slow or hung provider
    can never delay any other provider's discovery. A single poll is bounded by the
    two-threshold timeout from the provider's config: it logs a warning after
    ``discovery_warn_seconds`` and, if still unfinished after
    ``discovery_error_timeout_seconds``, emits a per-provider snapshot carrying a
    timeout ``DiscoveryError`` and moves on -- the abandoned discovery thread keeps
    running (threads cannot be killed) and its late result is accepted on a later
    poll. While a prior poll is still in flight, no new poll is started for that
    provider, so threads never pile up.
    """

    provider: BaseProviderInstance = Field(frozen=True)
    mngr_ctx: MngrContext = Field(frozen=True)
    config: ProviderInstanceConfig = Field(frozen=True)
    include_destroyed: bool = Field(default=True, frozen=True)

    _in_flight_future: Future[BoundedProviderDiscoveryResult] | None = PrivateAttr(default=None)
    _in_flight_started_at: datetime | None = PrivateAttr(default=None)
    # Carries in-flight per-host reads across this poller's polls so a wedged host is not
    # re-read every poll (bounding accumulation to at most one abandoned read per host).
    _host_read_registry: HostDiscoveryReadRegistry = PrivateAttr(default_factory=HostDiscoveryReadRegistry)

    @property
    def _discovered_provider(self) -> DiscoveredProvider:
        return make_discovered_provider(self.provider.name, self.config)

    def poll_and_emit(
        self,
        submit_discovery: Callable[[], "Future[BoundedProviderDiscoveryResult]"],
    ) -> None:
        """Run (or resume) one bounded discovery poll for this provider and write a snapshot.

        ``submit_discovery`` starts this provider's discovery in a background thread and
        returns a Future. It is supplied by ``run`` (bound to a long-lived executor) so
        that abandoning a timed-out discovery merely stops waiting -- the background
        thread keeps running and resolves the Future, whose late result is harvested on a
        subsequent poll. The Future captures any discovery exception (read via
        ``future.exception()``), so a failing provider becomes a per-provider error
        snapshot rather than propagating.
        """
        # If a previous poll's discovery is still running, only act once it finishes,
        # so we never run two concurrent discoveries for the same provider.
        if self._in_flight_future is not None:
            if self._in_flight_future.done():
                started_at = self._in_flight_started_at or _utc_now()
                self._harvest_and_emit(self._in_flight_future, started_at)
                self._in_flight_future = None
                self._in_flight_started_at = None
            return

        started_at = _utc_now()
        future = submit_discovery()
        # Two-threshold wait: warn first, then declare errored.
        if not _wait_for_future(future, self.config.discovery_warn_seconds):
            logger.warning(
                "Provider {} discovery is slow (still running after {:.0f}s)",
                self.provider.name,
                self.config.discovery_warn_seconds,
            )
            remaining = max(0.0, self.config.discovery_error_timeout_seconds - self.config.discovery_warn_seconds)
            if not _wait_for_future(future, remaining):
                self._emit_timeout_snapshot(started_at)
                # Keep the orphaned future; accept its late result on a later poll.
                self._in_flight_future = future
                self._in_flight_started_at = started_at
                return
        self._harvest_and_emit(future, started_at)

    def _harvest_and_emit(
        self,
        future: "Future[BoundedProviderDiscoveryResult]",
        started_at: datetime,
    ) -> None:
        """Emit a snapshot from a finished discovery future (success or error)."""
        # ``exception()`` reads the captured failure without re-raising it, so a failing
        # provider becomes an error snapshot rather than propagating out of the poll.
        error = future.exception()
        if error is not None:
            self._emit_error_snapshot(started_at, error)
            return
        result = future.result()
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider.name,
            agents=result.agents,
            hosts=result.hosts,
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            unknown_host_ids=result.unknown_host_ids,
            unknown_agent_ids=result.unknown_agent_ids,
        )

    def _emit_error_snapshot(self, started_at: datetime, exc: BaseException) -> None:
        cause = exc.__cause__ if isinstance(exc, ProviderError) and exc.__cause__ is not None else exc
        error = DiscoveryError(
            type_name=type(cause).__name__,
            message=str(cause),
            provider_name=self.provider.name,
        )
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider.name,
            agents=(),
            hosts=(),
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            error=error,
        )

    def _emit_timeout_snapshot(self, started_at: datetime) -> None:
        logger.warning(
            "Provider {} discovery timed out after {:.0f}s; emitting error snapshot and continuing",
            self.provider.name,
            self.config.discovery_error_timeout_seconds,
        )
        error = DiscoveryError(
            type_name="ProviderDiscoveryTimeoutError",
            message=(
                f"Discovery for provider '{self.provider.name}' did not complete within "
                f"{self.config.discovery_error_timeout_seconds:.0f}s"
            ),
            provider_name=self.provider.name,
        )
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider.name,
            agents=(),
            hosts=(),
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            error=error,
        )

    def run(self, stop_event: threading.Event) -> None:
        """Loop: poll, emit, then wait this provider's poll interval (until stopped).

        Holds one long-lived executor for the poller's lifetime; each poll submits the
        provider's discovery to it. The executor only runs one discovery at a time, but
        a poll never submits while a prior discovery is still in flight, so the abandoned
        (timed-out) discovery is never blocked by a new one.
        """
        with mngr_executor(
            parent_cg=self.mngr_ctx.concurrency_group,
            name=f"discover_provider_{self.provider.name}",
            max_workers=1,
        ) as executor:
            while not stop_event.is_set():
                # Expected transient failures (a failed snapshot write, a provider-config
                # error) must not kill this provider's poll loop; truly unexpected errors
                # propagate and stop only this poller (its thread is is_checked=False).
                try:
                    with log_span("Polling discovery for provider {}", self.provider.name):
                        self.poll_and_emit(
                            lambda: executor.submit(
                                _discover_one_provider,
                                self.provider,
                                self.mngr_ctx,
                                self.config.host_discovery_timeout_seconds,
                                self.config.agent_discovery_timeout_seconds,
                                self.include_destroyed,
                                self._host_read_registry,
                            )
                        )
                except (OSError, MngrError) as e:
                    logger.warning("Provider {} discovery poll failed (continuing): {}", self.provider.name, e)
                stop_event.wait(timeout=self.config.discovery_poll_interval_seconds)


def _wait_for_future(future: Future[BoundedProviderDiscoveryResult], timeout_seconds: float) -> bool:
    """Wait up to ``timeout_seconds`` for ``future``; return whether it completed."""
    done, _not_done = wait([future], timeout=timeout_seconds)
    return future in done


def run_per_provider_discovery_stream(
    mngr_ctx: MngrContext,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Stream discovery events as JSONL using independent per-provider poll loops.

    Replaces the single all-providers poll of ``run_discovery_stream``: each provider
    is polled on its own thread and cadence, writing :class:`ProviderDiscoverySnapshotEvent`
    lines to the shared discovery events file. A tail thread echoes every appended line
    (this process's own snapshots plus any events written by other mngr processes) to
    stdout or ``on_line``, deduplicated by event_id. Because each provider polls
    independently, a slow or hung provider cannot block discovery of any other.

    The set of providers is enumerated once at startup; a provider-set change is applied
    by restarting this process (e.g. minds bounces ``mngr observe`` on config change).
    """
    events_path = get_discovery_events_path(mngr_ctx.config)
    stop_event = threading.Event()
    emitted_event_ids: set[str] = set()
    emit_lock = threading.Lock()
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")

    # Start tailing from the current end of the file: per-provider snapshots written
    # below (and by other processes) are appended and picked up by the tail.
    initial_offset = events_path.stat().st_size if events_path.exists() else 0
    tail = threading.Thread(
        target=tail_discovery_events_from_offset,
        args=(events_path, initial_offset, stop_event, emitted_event_ids, emit_lock, warner, on_line),
        daemon=True,
    )
    tail.start()

    providers = get_all_provider_instances(mngr_ctx, None)
    pollers = [
        _ProviderDiscoveryPoller(
            provider=provider,
            mngr_ctx=mngr_ctx,
            config=_resolve_provider_config(provider, mngr_ctx),
        )
        for provider in providers
    ]
    # is_checked=False so one provider's poller crashing cannot fail the whole group
    # (and thus the other providers' pollers); on_failure logs which poller died.
    poller_threads = [
        mngr_ctx.concurrency_group.start_new_thread(
            target=poller.run,
            args=(stop_event,),
            daemon=True,
            name=f"discovery-poller-{poller.provider.name}",
            is_checked=False,
            on_failure=lambda exc, failed_poller=poller: logger.opt(exception=exc).error(
                "Discovery poller for provider {} crashed", failed_poller.provider.name
            ),
        )
        for poller in pollers
    ]

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for thread in poller_threads:
            thread.join(timeout=5.0)
        tail.join(timeout=5.0)
