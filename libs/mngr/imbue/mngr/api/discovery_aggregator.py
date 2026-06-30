import threading
from datetime import datetime
from enum import auto

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ProviderInstanceName


@pure
def parse_event_timestamp(timestamp: IsoTimestamp) -> datetime:
    """Parse an event envelope's nanosecond ISO timestamp into a timezone-aware datetime.

    ``datetime.fromisoformat`` accepts the trailing ``Z`` and truncates fractional
    seconds beyond microseconds, which is sufficient for ordering events against a
    snapshot's discovery span.
    """
    return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))


@pure
def is_intervening_event(last_event_at: datetime | None, discovery_started_at: datetime) -> bool:
    """True if a state-change/destroy event for an item was seen during a snapshot's span.

    An item whose most recent incremental event landed at or after the snapshot's
    ``discovery_started_at`` reflects newer truth than that in-flight snapshot, so
    the snapshot must not overwrite it.
    """
    return last_event_at is not None and last_event_at >= discovery_started_at


class RemovedItemDecision(UpperCaseStrEnum):
    """Whether a snapshot-absent item should be retained or dropped from tracking."""

    RETAIN = auto()
    DROP = auto()


@pure
def classify_removed_item(is_provider_errored: bool, has_intervening_event: bool) -> RemovedItemDecision:
    """Decide whether an item absent from a fresh per-provider snapshot is gone or merely unknown.

    Retain (do not forget) when the provider errored this poll -- its absence
    reflects the failed read, not a confirmed removal -- or when a newer
    incremental event for the item landed during the snapshot's span (so the
    snapshot's omission is stale). Otherwise the item is confirmed gone: drop it.
    """
    if is_provider_errored or has_intervening_event:
        return RemovedItemDecision.RETAIN
    return RemovedItemDecision.DROP


@pure
def should_apply_snapshot_item(has_intervening_event: bool) -> bool:
    """True if a snapshot's value for an item should be applied (not clobbering newer truth).

    A snapshot must not overwrite an item whose own state-change/destroy event was
    observed at or after the snapshot's ``discovery_started_at`` -- that event is
    newer than this in-flight snapshot.
    """
    return not has_intervening_event


class AggregatorDelta(FrozenModel):
    """Membership changes produced by applying a single discovery event.

    Lets consumers that manage per-agent/per-host resources (event streams,
    reverse tunnels) react to exactly what appeared or disappeared, without
    re-diffing the whole world. ``added`` ids became present; ``removed`` ids
    became absent. Items whose data merely changed are reported in neither set.
    """

    added_agent_ids: frozenset[str] = Field(default_factory=frozenset)
    removed_agent_ids: frozenset[str] = Field(default_factory=frozenset)
    added_host_ids: frozenset[str] = Field(default_factory=frozenset)
    removed_host_ids: frozenset[str] = Field(default_factory=frozenset)


class DiscoveryStateAggregator(MutableModel):
    """Accumulates per-provider discovery snapshots and incremental events into one consistent view.

    Replaces the per-consumer "one global snapshot is the whole world" reconciliation.
    Each :class:`ProviderDiscoverySnapshotEvent` is authoritative only for its own
    provider, so a snapshot's "what disappeared" diff is scoped to that provider's
    prior agents/hosts. Span-aware: an item whose own state-change/destroy event was
    observed during a snapshot's discovery span is never clobbered by that snapshot
    (see :func:`should_apply_snapshot_item` / :func:`classify_removed_item`).

    Thread-safe: every public method holds an internal lock, so a consumer may feed
    events from one thread while another reads the accumulated state.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _agent_by_id: dict[str, DiscoveredAgent] = PrivateAttr(default_factory=dict)
    _host_by_id: dict[str, DiscoveredHost] = PrivateAttr(default_factory=dict)
    _provider_name_by_agent_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _provider_name_by_host_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _provider_by_name: dict[ProviderInstanceName, DiscoveredProvider] = PrivateAttr(default_factory=dict)
    _error_by_provider_name: dict[ProviderInstanceName, DiscoveryError] = PrivateAttr(default_factory=dict)
    _unknown_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _unknown_host_ids: set[str] = PrivateAttr(default_factory=set)
    # Most recent incremental-event time per item, used to refuse clobbering by an
    # in-flight snapshot whose span the event falls within.
    _last_event_time_by_agent_id: dict[str, datetime] = PrivateAttr(default_factory=dict)
    _last_event_time_by_host_id: dict[str, datetime] = PrivateAttr(default_factory=dict)
    _last_event_at: datetime | None = PrivateAttr(default=None)
    _last_snapshot_at_by_provider: dict[ProviderInstanceName, datetime] = PrivateAttr(default_factory=dict)

    def apply_event(self, event: DiscoveryEvent) -> AggregatorDelta:
        """Fold one discovery event into the accumulated state, returning the membership delta.

        The legacy global :class:`FullDiscoverySnapshotEvent` is intentionally ignored
        -- consumers still on that path handle it themselves until they migrate.
        """
        match event:
            case ProviderDiscoverySnapshotEvent():
                return self._apply_provider_snapshot(event)
            case AgentDiscoveryEvent():
                return self._apply_agent_discovered(event)
            case HostDiscoveryEvent():
                return self._apply_host_discovered(event)
            case AgentDestroyedEvent():
                return self._apply_agent_destroyed(event)
            case HostDestroyedEvent():
                return self._apply_host_destroyed(event)
            case DiscoveryErrorEvent():
                return self._apply_discovery_error(event)
            case HostSSHInfoEvent():
                with self._lock:
                    self._bump_last_event_at(parse_event_timestamp(event.timestamp))
                return AggregatorDelta()
            case _:
                # Legacy FullDiscoverySnapshotEvent and any unmodeled type are ignored.
                return AggregatorDelta()

    def _apply_provider_snapshot(self, event: ProviderDiscoverySnapshotEvent) -> AggregatorDelta:
        with self._lock:
            before_agent_ids = frozenset(self._agent_by_id)
            before_host_ids = frozenset(self._host_by_id)

            provider_name = event.provider_name
            provider_str = str(provider_name)
            is_errored = event.error is not None

            # Merge this provider's construction + error state (never wholesale-replace
            # across providers -- other providers' state is untouched).
            if event.provider is not None:
                self._provider_by_name[provider_name] = event.provider
            if is_errored:
                self._error_by_provider_name[provider_name] = event.error
            else:
                self._error_by_provider_name.pop(provider_name, None)

            self._reconcile_provider_agents(event, provider_str, is_errored)
            self._reconcile_provider_hosts(event, provider_str, is_errored)

            self._last_snapshot_at_by_provider[provider_name] = event.discovery_finished_at
            self._bump_last_event_at(event.discovery_finished_at)

            return _membership_delta(
                before_agent_ids,
                frozenset(self._agent_by_id),
                before_host_ids,
                frozenset(self._host_by_id),
            )

    def _reconcile_provider_agents(
        self,
        event: ProviderDiscoverySnapshotEvent,
        provider_str: str,
        is_errored: bool,
    ) -> None:
        snapshot_agent_by_id = {str(agent.agent_id): agent for agent in event.agents}
        unknown_agent_ids = {str(agent_id) for agent_id in event.unknown_agent_ids}

        # Apply each snapshot agent unless a newer event already updated it.
        for agent_id_str, agent in snapshot_agent_by_id.items():
            has_intervening = is_intervening_event(
                self._last_event_time_by_agent_id.get(agent_id_str), event.discovery_started_at
            )
            if should_apply_snapshot_item(has_intervening):
                self._agent_by_id[agent_id_str] = agent
                self._provider_name_by_agent_id[agent_id_str] = provider_str
                self._unknown_agent_ids.discard(agent_id_str)

        # Reconcile agents we previously attributed to this provider that are absent
        # from the snapshot. Scope the diff to THIS provider so other providers'
        # agents are never touched.
        prior_provider_agent_ids = {
            agent_id_str for agent_id_str, name in self._provider_name_by_agent_id.items() if name == provider_str
        }
        removed_agent_ids = prior_provider_agent_ids - set(snapshot_agent_by_id)
        for agent_id_str in removed_agent_ids:
            if agent_id_str in unknown_agent_ids:
                # Explicitly unknown (sub-provider timeout): retain prior data, mark unknown.
                self._unknown_agent_ids.add(agent_id_str)
                continue
            has_intervening = is_intervening_event(
                self._last_event_time_by_agent_id.get(agent_id_str), event.discovery_started_at
            )
            decision = classify_removed_item(is_errored, has_intervening)
            if decision is RemovedItemDecision.DROP:
                self._forget_agent(agent_id_str)
            elif is_errored:
                # Provider errored: keep the agent but surface it as unknown/stale.
                self._unknown_agent_ids.add(agent_id_str)
            else:
                # Retained because a newer event landed during the snapshot's span:
                # that event already set the agent's state, so leave it as-is.
                pass

    def _reconcile_provider_hosts(
        self,
        event: ProviderDiscoverySnapshotEvent,
        provider_str: str,
        is_errored: bool,
    ) -> None:
        snapshot_host_by_id = {str(host.host_id): host for host in event.hosts}
        unknown_host_ids = {str(host_id) for host_id in event.unknown_host_ids}

        for host_id_str, host in snapshot_host_by_id.items():
            has_intervening = is_intervening_event(
                self._last_event_time_by_host_id.get(host_id_str), event.discovery_started_at
            )
            if should_apply_snapshot_item(has_intervening):
                self._host_by_id[host_id_str] = host
                self._provider_name_by_host_id[host_id_str] = provider_str
                self._unknown_host_ids.discard(host_id_str)

        prior_provider_host_ids = {
            host_id_str for host_id_str, name in self._provider_name_by_host_id.items() if name == provider_str
        }
        removed_host_ids = prior_provider_host_ids - set(snapshot_host_by_id)
        for host_id_str in removed_host_ids:
            if host_id_str in unknown_host_ids:
                self._unknown_host_ids.add(host_id_str)
                continue
            has_intervening = is_intervening_event(
                self._last_event_time_by_host_id.get(host_id_str), event.discovery_started_at
            )
            decision = classify_removed_item(is_errored, has_intervening)
            if decision is RemovedItemDecision.DROP:
                self._forget_host(host_id_str)
            elif is_errored:
                self._unknown_host_ids.add(host_id_str)
            else:
                # Retained because a newer event landed during the snapshot's span:
                # that event already set the host's state, so leave it as-is.
                pass

    def _apply_agent_discovered(self, event: AgentDiscoveryEvent) -> AggregatorDelta:
        event_at = parse_event_timestamp(event.timestamp)
        agent = event.agent
        agent_id_str = str(agent.agent_id)
        with self._lock:
            was_present = agent_id_str in self._agent_by_id
            self._agent_by_id[agent_id_str] = agent
            self._provider_name_by_agent_id[agent_id_str] = str(agent.provider_name)
            self._unknown_agent_ids.discard(agent_id_str)
            self._last_event_time_by_agent_id[agent_id_str] = event_at
            self._bump_last_event_at(event_at)
            added = frozenset() if was_present else frozenset({agent_id_str})
            return AggregatorDelta(added_agent_ids=added)

    def _apply_host_discovered(self, event: HostDiscoveryEvent) -> AggregatorDelta:
        event_at = parse_event_timestamp(event.timestamp)
        host = event.host
        host_id_str = str(host.host_id)
        with self._lock:
            was_present = host_id_str in self._host_by_id
            self._host_by_id[host_id_str] = host
            self._provider_name_by_host_id[host_id_str] = str(host.provider_name)
            self._unknown_host_ids.discard(host_id_str)
            self._last_event_time_by_host_id[host_id_str] = event_at
            self._bump_last_event_at(event_at)
            added = frozenset() if was_present else frozenset({host_id_str})
            return AggregatorDelta(added_host_ids=added)

    def _apply_agent_destroyed(self, event: AgentDestroyedEvent) -> AggregatorDelta:
        event_at = parse_event_timestamp(event.timestamp)
        agent_id_str = str(event.agent_id)
        with self._lock:
            was_present = agent_id_str in self._agent_by_id
            self._forget_agent(agent_id_str)
            # Record the destroy time so a snapshot whose span predates it cannot resurrect the agent.
            self._last_event_time_by_agent_id[agent_id_str] = event_at
            self._bump_last_event_at(event_at)
            removed = frozenset({agent_id_str}) if was_present else frozenset()
            return AggregatorDelta(removed_agent_ids=removed)

    def _apply_host_destroyed(self, event: HostDestroyedEvent) -> AggregatorDelta:
        event_at = parse_event_timestamp(event.timestamp)
        host_id_str = str(event.host_id)
        agent_id_strs = [str(agent_id) for agent_id in event.agent_ids]
        with self._lock:
            removed_host = frozenset({host_id_str}) if host_id_str in self._host_by_id else frozenset()
            self._forget_host(host_id_str)
            self._last_event_time_by_host_id[host_id_str] = event_at
            removed_agents: set[str] = set()
            for agent_id_str in agent_id_strs:
                if agent_id_str in self._agent_by_id:
                    removed_agents.add(agent_id_str)
                self._forget_agent(agent_id_str)
                self._last_event_time_by_agent_id[agent_id_str] = event_at
            self._bump_last_event_at(event_at)
            return AggregatorDelta(removed_host_ids=removed_host, removed_agent_ids=frozenset(removed_agents))

    def _apply_discovery_error(self, event: DiscoveryErrorEvent) -> AggregatorDelta:
        event_at = parse_event_timestamp(event.timestamp)
        with self._lock:
            if event.provider_name is not None:
                provider_name = ProviderInstanceName(event.provider_name)
                self._error_by_provider_name[provider_name] = DiscoveryError(
                    type_name=event.error_type,
                    message=event.error_message,
                    provider_name=provider_name,
                )
            self._bump_last_event_at(event_at)
            return AggregatorDelta()

    def _forget_agent(self, agent_id_str: str) -> None:
        self._agent_by_id.pop(agent_id_str, None)
        self._provider_name_by_agent_id.pop(agent_id_str, None)
        self._unknown_agent_ids.discard(agent_id_str)

    def _forget_host(self, host_id_str: str) -> None:
        self._host_by_id.pop(host_id_str, None)
        self._provider_name_by_host_id.pop(host_id_str, None)
        self._unknown_host_ids.discard(host_id_str)

    def _bump_last_event_at(self, event_at: datetime) -> None:
        if self._last_event_at is None or event_at > self._last_event_at:
            self._last_event_at = event_at

    # === Query API (each returns a fresh copy; safe to read concurrently) ===

    def get_agents(self) -> list[DiscoveredAgent]:
        with self._lock:
            return list(self._agent_by_id.values())

    def get_agent_by_id(self) -> dict[str, DiscoveredAgent]:
        with self._lock:
            return dict(self._agent_by_id)

    def get_hosts(self) -> list[DiscoveredHost]:
        with self._lock:
            return list(self._host_by_id.values())

    def get_host_by_id(self) -> dict[str, DiscoveredHost]:
        with self._lock:
            return dict(self._host_by_id)

    def get_providers(self) -> list[DiscoveredProvider]:
        with self._lock:
            return list(self._provider_by_name.values())

    def get_error_by_provider_name(self) -> dict[ProviderInstanceName, DiscoveryError]:
        with self._lock:
            return dict(self._error_by_provider_name)

    def get_unknown_agent_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._unknown_agent_ids)

    def get_unknown_host_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._unknown_host_ids)

    def get_last_event_at(self) -> datetime | None:
        with self._lock:
            return self._last_event_at

    def get_last_snapshot_at_for_provider(self, provider_name: ProviderInstanceName) -> datetime | None:
        with self._lock:
            return self._last_snapshot_at_by_provider.get(provider_name)

    def get_last_snapshot_at(self) -> datetime | None:
        """The most recent per-provider snapshot time across all providers.

        An aggregate freshness fallback for callers that have no single provider
        in mind; prefer :meth:`get_last_snapshot_at_for_provider` when scoping to
        a workspace's provider.
        """
        with self._lock:
            if not self._last_snapshot_at_by_provider:
                return None
            return max(self._last_snapshot_at_by_provider.values())


@pure
def _membership_delta(
    before_agent_ids: frozenset[str],
    after_agent_ids: frozenset[str],
    before_host_ids: frozenset[str],
    after_host_ids: frozenset[str],
) -> AggregatorDelta:
    """Compute appeared/disappeared id sets between two membership snapshots."""
    return AggregatorDelta(
        added_agent_ids=after_agent_ids - before_agent_ids,
        removed_agent_ids=before_agent_ids - after_agent_ids,
        added_host_ids=after_host_ids - before_host_ids,
        removed_host_ids=before_host_ids - after_host_ids,
    )
