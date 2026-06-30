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
from imbue.mngr.api.discovery_events import _discovery_stream_tail_events_file
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import write_provider_discovery_snapshot
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import ProviderError
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.thread_cleanup import cleanup_thread_local_resources


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
    include_destroyed: bool,
) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
    """Run a single provider's discovery, returning its host->agents map. Raises on failure."""
    return provider.discover_hosts_and_agents(cg=mngr_ctx.concurrency_group, include_destroyed=include_destroyed)


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

    _in_flight_future: Future[dict[DiscoveredHost, list[DiscoveredAgent]]] | None = PrivateAttr(default=None)
    _in_flight_started_at: datetime | None = PrivateAttr(default=None)

    @property
    def _discovered_provider(self) -> DiscoveredProvider:
        return make_discovered_provider(self.provider.name, self.config)

    def poll_and_emit(self) -> None:
        """Run (or resume) one bounded discovery poll for this provider and write a snapshot."""
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
        future = self._start_discovery()
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

    def _start_discovery(self) -> "Future[dict[DiscoveredHost, list[DiscoveredAgent]]]":
        """Run this provider's discovery in a background cg thread, returning a Future.

        Uses a raw thread + manually-resolved Future rather than a pooled executor so
        that abandoning a timed-out discovery simply stops waiting -- the thread keeps
        running and resolves the Future later (its late result is harvested on a
        subsequent poll). Mirrors the per-task gevent-hub cleanup that ``mngr_executor``
        performs, since discovery may run pyinfra ops in this thread.
        """
        future: Future[dict[DiscoveredHost, list[DiscoveredAgent]]] = Future()

        def _run() -> None:
            # Capture any failure into the Future (the standard executor pattern) so the
            # poller can attribute it to this provider rather than failing the cg strand.
            try:
                result = _discover_one_provider(self.provider, self.mngr_ctx, self.include_destroyed)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                cleanup_thread_local_resources()

        self.mngr_ctx.concurrency_group.start_new_thread(
            target=_run,
            name=f"discover_provider_{self.provider.name}",
            daemon=True,
            is_checked=False,
        )
        return future

    def _harvest_and_emit(
        self,
        future: "Future[dict[DiscoveredHost, list[DiscoveredAgent]]]",
        started_at: datetime,
    ) -> None:
        """Emit a snapshot from a finished discovery future (success or error)."""
        try:
            agents_by_host = future.result()
        except Exception as exc:
            self._emit_error_snapshot(started_at, exc)
            return
        agents: list[DiscoveredAgent] = []
        hosts: list[DiscoveredHost] = []
        for host_ref, agent_refs in agents_by_host.items():
            hosts.append(host_ref)
            agents.extend(agent_refs)
        write_provider_discovery_snapshot(
            self.mngr_ctx.config,
            provider_name=self.provider.name,
            agents=agents,
            hosts=hosts,
            discovery_started_at=started_at,
            discovery_finished_at=_utc_now(),
            provider=self._discovered_provider,
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
        """Loop: poll, emit, then wait this provider's poll interval (until stopped)."""
        while not stop_event.is_set():
            try:
                with log_span("Polling discovery for provider {}", self.provider.name):
                    self.poll_and_emit()
            except Exception as e:
                logger.opt(exception=e).error("Provider {} discovery poll failed (continuing)", self.provider.name)
            stop_event.wait(timeout=self.config.discovery_poll_interval_seconds)


def _wait_for_future(future: Future[dict[DiscoveredHost, list[DiscoveredAgent]]], timeout_seconds: float) -> bool:
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
        target=_discovery_stream_tail_events_file,
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
    poller_threads = [
        mngr_ctx.concurrency_group.start_new_thread(
            target=poller.run,
            args=(stop_event,),
            daemon=True,
            name=f"discovery-poller-{poller.provider.name}",
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
