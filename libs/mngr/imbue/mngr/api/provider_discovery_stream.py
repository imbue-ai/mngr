import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import wait
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import emit_host_ssh_info
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import tail_discovery_events_from_offset
from imbue.mngr.api.discovery_events import write_provider_discovery_snapshot
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.providers import list_provider_names_to_load
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import BoundedProviderDiscoveryResult
from imbue.mngr.interfaces.provider_instance import HostDiscoveryReadRegistry
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.error_utils import format_exception_traceback
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.thread_cleanup import mngr_executor

# How long the stream's main loop blocks per wait. The main thread has nothing
# to do but notice a stop -- Ctrl-C interrupts the wait directly and stop_event
# wakes it -- so this is a liveness backstop rather than a poll interval; a
# short value would only burn idle wakeups (which cost ~3x under gVisor).
_SHUTDOWN_CHECK_INTERVAL_SECONDS: Final[float] = 3600.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_provider_config(provider_name: ProviderInstanceName, mngr_ctx: MngrContext) -> ProviderInstanceConfig:
    """Return the configured block for a provider, or a default block for implicit-default instances.

    Mirrors the default-config fallback used by the listing path: an implicit-default
    provider (no explicit ``[providers.<name>]`` block) uses its name as the backend.
    Resolvable from config alone, so it also works for providers whose instance
    construction was skipped (unauthorized/unavailable/empty).
    """
    explicit = mngr_ctx.config.providers.get(provider_name)
    if explicit is not None:
        return explicit
    return ProviderInstanceConfig(backend=ProviderBackendName(str(provider_name)))


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
    # Each poll is its own discovery cycle, so clear the provider's per-cycle caches
    # first. The poller builds its instance on every poll, but ``get_provider_instance``
    # memoizes the first successful build, so in practice one instance serves every
    # poll; without this reset its per-cycle caches (e.g. imbue_cloud's leased-hosts
    # list, which is even cached when empty) become process-lifetime caches, so any
    # later change -- a host leased afterwards, a host destroyed -- is never seen.
    # Every on-demand discovery path (mngr list, the snapshot side-effect) already
    # passes reset_caches=True for exactly this reason; the streaming poller must match.
    # The cross-poll wedged-host registry is a separate object and is intentionally
    # left untouched.
    provider.reset_caches()
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
    provider, so threads never pile up; the timeout snapshot is re-emitted instead,
    so every cycle of a live poller produces a snapshot no matter what the provider
    is doing.

    The poller holds a provider *name*, not an instance, and builds the instance on
    each poll (``get_provider_instance`` memoizes the successful build, so this costs
    one lookup once the provider is up). Construction is where a provider reports
    itself unreachable or not-yet-created -- Docker Desktop paused, or no Docker state
    container / Modal environment yet -- and it is retried because those conditions end:
    the user unpauses Docker, or creates their first agent. Previously the stream
    built every instance once at startup, so a provider that failed *there* got a single
    snapshot and no poller for the life of the process, while one that failed on every
    *poll* kept its poller and recovered on its own. That asymmetry is what this removes.
    """

    provider_name: ProviderInstanceName = Field(frozen=True)
    mngr_ctx: MngrContext = Field(frozen=True)
    config: ProviderInstanceConfig = Field(frozen=True)
    include_destroyed: bool = Field(default=True, frozen=True)

    _in_flight_future: Future[BoundedProviderDiscoveryResult] | None = PrivateAttr(default=None)
    _in_flight_started_at: datetime | None = PrivateAttr(default=None)
    # Carries in-flight per-host reads across this poller's polls so a wedged host is not
    # re-read every poll (bounding accumulation to at most one abandoned read per host).
    _host_read_registry: HostDiscoveryReadRegistry = PrivateAttr(default_factory=HostDiscoveryReadRegistry)
    # Whether the last poll's construction failed, so a repeat of the same failure logs
    # quietly. Cleared as soon as the provider builds.
    _is_construction_failing: bool = PrivateAttr(default=False)
    # Set when the provider reports missing credentials, which no retry can fix.
    _is_ended_pending_credentials: bool = PrivateAttr(default=False)

    @property
    def _discovered_provider(self) -> DiscoveredProvider:
        return make_discovered_provider(self.provider_name, self.config)

    def poll_and_emit(
        self,
        submit_discovery: Callable[[BaseProviderInstance], "Future[BoundedProviderDiscoveryResult]"],
    ) -> None:
        """Run (or resume) one bounded discovery poll for this provider and write a snapshot.

        ``submit_discovery`` starts this provider's discovery in a background thread and
        returns a Future. It is supplied by ``run`` (bound to a long-lived executor) so
        that abandoning a timed-out discovery merely stops waiting -- the background
        thread keeps running and resolves the Future, whose late result is harvested on a
        subsequent poll. The Future captures any discovery exception (read via
        ``future.exception()``), so a failing provider becomes a per-provider error
        snapshot rather than propagating.

        Every path through this method writes exactly one snapshot, including the one
        where the provider cannot be built at all: a poller that emits nothing is
        indistinguishable from a poller that died.
        """
        # If a previous poll's discovery is still running, never start a second one
        # for this provider -- but still emit, because a poller that is alive must be
        # visible as alive. Going quiet here would make a wedged provider (which can
        # stay wedged indefinitely) indistinguishable from a dead one.
        if self._in_flight_future is not None:
            in_flight_started_at = self._in_flight_started_at or _utc_now()
            if not self._in_flight_future.done():
                self._emit_timeout_snapshot(in_flight_started_at, is_newly_timed_out=False)
                return
            self._harvest_and_emit(self._in_flight_future, in_flight_started_at)
            self._in_flight_future = None
            self._in_flight_started_at = None
            return

        started_at = _utc_now()
        provider = self._build_provider_or_emit_skip(started_at)
        if provider is None:
            return
        future = submit_discovery(provider)
        # Two-threshold wait: warn first, then declare errored.
        if not _wait_for_future(future, self.config.discovery_warn_seconds):
            logger.warning(
                "Provider {} discovery is slow (still running after {:.0f}s)",
                self.provider_name,
                self.config.discovery_warn_seconds,
            )
            remaining = max(0.0, self.config.discovery_error_timeout_seconds - self.config.discovery_warn_seconds)
            if not _wait_for_future(future, remaining):
                self._emit_timeout_snapshot(started_at, is_newly_timed_out=True)
                # Keep the orphaned future; accept its late result on a later poll.
                self._in_flight_future = future
                self._in_flight_started_at = started_at
                return
        self._harvest_and_emit(future, started_at)

    def _build_provider_or_emit_skip(self, started_at: datetime) -> BaseProviderInstance | None:
        """Build this provider's instance, or emit its construction failure and return None.

        A backend reports two recoverable conditions by raising from construction:
        ``ProviderEmptyError`` (reached, and definitively holds nothing yet) and
        ``ProviderUnavailableError`` (could not be reached at all). Both end on their
        own -- the user creates a first agent, or unpauses Docker -- so both are
        snapshotted and retried rather than being treated as the provider's final word.
        They are siblings, not one subclass of the other, so both have to be named here.

        The retry is at the provider's ordinary poll cadence, with no extra backoff on
        top. A provider that fails to build does strictly *less* work per poll than one
        that succeeds: a successful build is cached, so a healthy provider spends its
        poll on a full ``discover_hosts_and_agents_within_timeouts`` (caches reset first,
        so it really re-reads), while a failing one spends it on the construction check
        alone -- one lookup for modal's missing environment, an immediate ECONNREFUSED
        for a paused Docker. Backing off would buy less than a working provider already
        costs, and would pay for it in the only latency the user actually feels: how long
        the app waits on an empty provider after Docker comes back.
        ``discovery_poll_interval_seconds`` is already the per-provider knob for how
        often it is acceptable to talk to a backend, and it governs the expensive case.

        Any other ``MngrError`` or ``OSError`` is snapshotted the same way, because
        the snapshot is what tells a consumer this poller is alive. The reachable case
        is a config block naming a backend this install does not have
        (``UnknownBackendError``): a name that ``list_provider_names_to_load`` returns
        without checking the registry, and one no retry can repair inside a running
        interpreter. Retrying it anyway costs a registry lookup per poll and buys the one
        thing that matters -- the provider keeps reporting, instead of dropping out of
        the stream while every other provider keeps writing to it.

        ``ProviderNotAuthorizedError`` is the one exception. It *is* a
        ``ProviderUnavailableError`` subclass, but credentials only change through a
        user action that restarts this process anyway, so retrying it just re-reports
        the same thing forever. Its snapshot is emitted once and this poller ends --
        by returning, never by raising: ``run``'s exit reads a live exception as a
        crashed poller and would take the whole process down over one provider's
        missing API key.
        """
        try:
            provider = get_provider_instance(self.provider_name, self.mngr_ctx)
        except ProviderNotAuthorizedError as e:
            logger.warning(
                "Provider {} is not authorized ({}); emitting its snapshot and ending its discovery poller",
                self.provider_name,
                e,
            )
            self._emit_construction_skip_snapshot(started_at, e, is_empty=False)
            self._is_ended_pending_credentials = True
            return None
        except (ProviderEmptyError, ProviderUnavailableError, OSError, MngrError) as e:
            is_empty = isinstance(e, ProviderEmptyError)
            self._log_construction_skip(e, is_empty=is_empty)
            self._emit_construction_skip_snapshot(started_at, e, is_empty=is_empty)
            self._is_construction_failing = True
            return None
        self._is_construction_failing = False
        return provider

    def _log_construction_skip(self, exc: BaseException, is_empty: bool) -> None:
        """Log a retryable construction failure, at warning only the first time it happens.

        The repeats that follow restate the same fact once per poll for as long as the
        condition lasts -- which can be the whole life of the process -- and would drown
        the log. Mirrors the wedged-provider re-emission.
        """
        if self._is_construction_failing:
            logger.debug("Provider {} still cannot be built; re-emitting its snapshot: {}", self.provider_name, exc)
        elif is_empty:
            logger.info(
                "Provider {} has nothing yet ({}); emitting an empty snapshot and retrying next poll",
                self.provider_name,
                exc,
            )
        else:
            logger.warning(
                "Provider {} could not be built ({}); emitting its snapshot and retrying next poll",
                self.provider_name,
                exc,
            )

    def _emit_construction_skip_snapshot(self, started_at: datetime, exc: BaseException, is_empty: bool) -> None:
        """Write the snapshot for a provider that could not be built this poll.

        A known-empty provider gets a clean zero-agent snapshot -- that is a healthy
        state, not an error. Anything else carries its construction error, so the minds
        providers panel can show what is wrong. No traceback: unlike an arbitrary poll
        failure, these errors state their own cause and remediation, and this line is
        now written on every poll of a broken provider.
        """
        error = (
            None
            if is_empty
            else DiscoveryError(
                type_name=type(exc).__name__,
                message=str(exc),
                provider_name=self.provider_name,
            )
        )
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider_name,
            agents=(),
            hosts=(),
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            error=error,
        )

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
            provider_name=self.provider_name,
            agents=result.agents,
            hosts=result.hosts,
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            unknown_host_ids=result.unknown_host_ids,
            unknown_agent_ids=result.unknown_agent_ids,
        )
        # Re-emit each host's SSH endpoint so consumers that tunnel to the host (the minds
        # system_interface forward) get it from the streaming path. Only a full ``mngr list``
        # emits these otherwise, which the running app never does periodically, so without this
        # a forward that loses a host's SSH info (e.g. after the host briefly left discovery)
        # never regains it and refuses to dial the host's loopback-registered service URL.
        for host_id, ssh_info in result.host_ssh_infos:
            emit_host_ssh_info(self.mngr_ctx.config, host_id, ssh_info)

    def _emit_error_snapshot(self, started_at: datetime, exc: BaseException) -> None:
        cause = exc.__cause__ if isinstance(exc, ProviderError) and exc.__cause__ is not None else exc
        # The snapshot is the only durable record of this failure -- nothing logs a
        # traceback around it -- so carry one, matching the cause we name above.
        error = DiscoveryError(
            type_name=type(cause).__name__,
            message=str(cause),
            provider_name=self.provider_name,
            traceback_text=format_exception_traceback(cause),
        )
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider_name,
            agents=(),
            hosts=(),
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
            error=error,
        )

    def _emit_timeout_snapshot(self, started_at: datetime, is_newly_timed_out: bool) -> None:
        """Write this provider's timed-out snapshot, re-emitting for as long as it stays wedged.

        ``is_newly_timed_out`` only picks the log level: the first timeout of an
        episode is worth a warning, while the repeats that follow it are the same
        fact restated once per poll interval and would drown the log.
        """
        if is_newly_timed_out:
            logger.warning(
                "Provider {} discovery timed out after {:.0f}s; emitting error snapshot and continuing",
                self.provider_name,
                self.config.discovery_error_timeout_seconds,
            )
        else:
            logger.debug(
                "Provider {} discovery is still running since {}; re-emitting its timeout snapshot",
                self.provider_name,
                started_at,
            )
        error = DiscoveryError(
            type_name="ProviderDiscoveryTimeoutError",
            message=(
                f"Discovery for provider '{self.provider_name}' did not complete within "
                f"{self.config.discovery_error_timeout_seconds:.0f}s"
            ),
            provider_name=self.provider_name,
        )
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider_name,
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

        Returns normally when the provider turns out to need credentials the user
        has not supplied. That is the one condition polling cannot resolve, and it
        belongs to one provider, so it ends one poller and leaves every other
        provider (and this process) running.
        """
        with mngr_executor(
            parent_cg=self.mngr_ctx.concurrency_group,
            name=f"discover_provider_{self.provider_name}",
            max_workers=1,
        ) as executor:
            while not stop_event.is_set():
                # Expected transient failures (a failed snapshot write, a provider-config
                # error) must not kill this provider's poll loop.
                try:
                    with log_span("Polling discovery for provider {}", self.provider_name):
                        self.poll_and_emit(
                            lambda provider: executor.submit(
                                _discover_one_provider,
                                provider,
                                self.mngr_ctx,
                                self.config.host_discovery_timeout_seconds,
                                self.config.agent_discovery_timeout_seconds,
                                self.include_destroyed,
                                self._host_read_registry,
                            )
                        )
                except (OSError, MngrError) as e:
                    logger.warning("Provider {} discovery poll failed (continuing): {}", self.provider_name, e)
                if self._is_ended_pending_credentials:
                    return
                stop_event.wait(timeout=self.config.discovery_poll_interval_seconds)


def _wait_for_future(future: Future[BoundedProviderDiscoveryResult], timeout_seconds: float) -> bool:
    """Wait up to ``timeout_seconds`` for ``future``; return whether it completed."""
    done, _not_done = wait([future], timeout=timeout_seconds)
    return future in done


def _start_poller_thread(
    provider_name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    stop_event: threading.Event,
) -> ObservableThread:
    """Start one provider's poll loop on its own thread."""
    poller = _ProviderDiscoveryPoller(
        provider_name=provider_name,
        mngr_ctx=mngr_ctx,
        config=_resolve_provider_config(provider_name, mngr_ctx),
    )
    # is_checked=False so one provider's poller crashing cannot fail the whole group
    # (and thus the other providers' pollers); on_failure logs which poller died.
    return mngr_ctx.concurrency_group.start_new_thread(
        target=poller.run,
        args=(stop_event,),
        daemon=True,
        name=f"discovery-poller-{provider_name}",
        is_checked=False,
        on_failure=lambda exc: logger.opt(exception=exc).error(
            "Discovery poller for provider {} crashed", provider_name
        ),
    )


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

    The set of provider *names* is enumerated once at startup; a provider-set change is
    applied by restarting this process (e.g. minds bounces ``mngr observe`` on config
    change). Every enumerated name gets a poller, including one whose instance cannot be
    built right now: construction happens inside the poller and is retried, so a provider
    that is merely unreachable or not-yet-created at launch is not written off for the
    life of the process.
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

    # Names, not instances: each poller builds its own provider, so nothing here is
    # skipped for failing to construct, and no provider's first poll waits behind
    # another's slow construction.
    provider_names = list_provider_names_to_load(mngr_ctx)
    poller_threads = [_start_poller_thread(provider_name, mngr_ctx, stop_event) for provider_name in provider_names]

    try:
        # Each poller drives itself, so there is nothing to do here but wait for shutdown.
        while not stop_event.wait(timeout=_SHUTDOWN_CHECK_INTERVAL_SECONDS):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for poller_thread in poller_threads:
            poller_thread.join(timeout=5.0)
        tail.join(timeout=5.0)
