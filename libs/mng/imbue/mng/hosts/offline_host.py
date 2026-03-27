from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.data_types import ActivityConfig
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName


def validate_and_create_discovered_agent(
    agent_data: dict[str, Any],
    host_id: HostId,
    provider_name: ProviderInstanceName,
) -> DiscoveredAgent | None:
    """Validate agent data and create a DiscoveredAgent if valid.

    Returns None if the agent data is malformed (missing or invalid id/name).
    Logs warnings for malformed records.
    """
    agent_id_str = agent_data.get("id")
    if agent_id_str is None:
        logger.warning("Skipping malformed agent record for host {}: missing 'id': {}", host_id, agent_data)
        return None
    try:
        agent_id = AgentId(agent_id_str)
    except ValueError as e:
        logger.opt(exception=e).warning(
            "Skipping malformed agent record for host {}: invalid 'id': {}", host_id, agent_data
        )
        return None

    agent_name_str = agent_data.get("name")
    if agent_name_str is None:
        logger.warning("Skipping malformed agent record for host {}: missing 'name': {}", host_id, agent_data)
        return None
    try:
        agent_name = AgentName(agent_name_str)
    except ValueError as e:
        logger.opt(exception=e).warning(
            "Skipping malformed agent record for host {}: invalid 'name': {}", host_id, agent_data
        )
        return None

    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=agent_name,
        provider_name=provider_name,
        certified_data=agent_data,
    )


class BaseHost(HostInterface):
    """Base for host implementations (shared between offline and online hosts)."""

    provider_instance: ProviderInstanceInterface = Field(
        frozen=True, description="The provider instance managing this host"
    )
    mng_ctx: MngContext = Field(frozen=True, repr=False, description="The mng context")
    on_updated_host_data: Callable[[HostId, CertifiedHostData], None] | None = Field(
        frozen=True,
        default=None,
        description="Optional callback invoked when certified host data is updated",
    )

    @property
    def host_dir(self) -> Path:
        """Get the host state directory path from provider instance."""
        return self.provider_instance.host_dir

    # =========================================================================
    # Activity Configuration
    # =========================================================================

    def get_activity_config(self) -> ActivityConfig:
        """Get the activity configuration for this host."""
        certified_data = self.get_certified_data()
        return ActivityConfig(
            idle_timeout_seconds=certified_data.idle_timeout_seconds,
            activity_sources=certified_data.activity_sources,
        )

    def set_activity_config(self, config: ActivityConfig) -> None:
        """Set the activity configuration for this host.

        Saves activity configuration to data.json, which is read by the
        activity_watcher.sh script using jq.
        """
        with log_span(
            "Setting activity config for host {}: idle_timeout={}s, activity_sources={}",
            self.id,
            config.idle_timeout_seconds,
            config.activity_sources,
        ):
            certified_data = self.get_certified_data()
            updated_data = certified_data.model_copy_update(
                to_update(certified_data.field_ref().idle_timeout_seconds, config.idle_timeout_seconds),
                to_update(certified_data.field_ref().activity_sources, config.activity_sources),
            )
            self.set_certified_data(updated_data)

    # =========================================================================
    # Certified Data
    # =========================================================================

    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        """Get certified plugin data from data.json."""
        certified_data = self.get_certified_data()
        return certified_data.plugin.get(plugin_name, {})

    # =========================================================================
    # Provider-Derived Information
    # =========================================================================

    def get_snapshots(self) -> list[SnapshotInfo]:
        """Get list of snapshots from the provider."""
        return self.provider_instance.list_snapshots(self)

    def get_image(self) -> str | None:
        """Get the image used for this host."""
        all_data = self.get_certified_data()
        return all_data.image

    def get_tags(self) -> dict[str, str]:
        """Get tags from the provider."""
        all_data = self.get_certified_data()
        return {**all_data.user_tags}

    # =========================================================================
    # Agent Information
    # =========================================================================

    def _validate_and_create_discovered_agent(self, agent_data: dict[str, Any]) -> DiscoveredAgent | None:
        """Validate agent data and create a DiscoveredAgent if valid.

        Returns None if the agent data is malformed (missing or invalid id/name).
        Logs warnings for malformed records.
        """
        return validate_and_create_discovered_agent(agent_data, self.id, self.provider_instance.name)

    def discover_agents(self) -> list[DiscoveredAgent]:
        """Return a list of all agent references for this host.

        For offline hosts, get agent information from the provider's persisted data.
        The full agent data.json contents are included as certified_data.
        Malformed agent records are skipped with a log.
        """
        agent_records = self.provider_instance.list_persisted_agent_data_for_host(self.id)

        agent_refs: list[DiscoveredAgent] = []
        for agent_data in agent_records:
            ref = self._validate_and_create_discovered_agent(agent_data)
            if ref is not None:
                agent_refs.append(ref)

        return agent_refs

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================
    def get_state(self) -> HostState:
        """Get the current state of the host.

        For offline hosts, we determine state based on certified data, stop_reason, and snapshots:
        - If certified data has a failure_reason, the host failed during creation
        - If snapshots exist:
          - stop_reason=PAUSED -> host became idle and was paused
          - stop_reason=STOPPED -> user explicitly stopped all agents on the host
          - stop_reason=None -> host crashed (no controlled shutdown recorded)
        - If no snapshots exist for a provider that supports them, the host is DESTROYED
        - If provider doesn't support snapshots, assume STOPPED
        """
        certified_data = self.get_certified_data()
        if certified_data.failure_reason is not None:
            return HostState.FAILED

        # Determine state based on stop_reason
        stop_reason = certified_data.stop_reason
        if stop_reason is None:
            return HostState.CRASHED

        if self.provider_instance.supports_shutdown_hosts:
            # if the provider normally allows hosts to be shutdown, the reason is fine
            return HostState(stop_reason)

        # if we cannot resume, and we don't support snapshots, this must be destroyed
        if not self.provider_instance.supports_snapshots:
            return HostState.DESTROYED

        # otherwise, check if we have any snapshots
        snapshots = self.get_snapshots()
        # if we don't, I guess this is destroyed
        if not snapshots:
            return HostState.DESTROYED

        # ok, the stored state is fine!
        return HostState(stop_reason)

    def get_failure_reason(self) -> str | None:
        """Get the failure reason if this host failed during creation."""
        return self.get_certified_data().failure_reason

    def get_build_log(self) -> str | None:
        """Get the build log if this host failed during creation."""
        return self.get_certified_data().build_log

    def get_permissions(self) -> list[str]:
        """Get the union of all agent permissions on this host.

        Uses persisted agent data from the provider to get permissions without
        requiring the host to be online.
        """
        permissions: set[str] = set()
        for agent_ref in self.discover_agents():
            permissions.update(str(p) for p in agent_ref.permissions)
        return list(permissions)


class OfflineHost(BaseHost):
    """Host implementation that uses json data to enable reading the state of a host that is now offline.

    This is used when we have stored data about a host (e.g., from provider metadata or persisted
    agent data) but cannot currently connect to it. It provides read-only access to the host's
    last-known state.
    """

    certified_host_data: CertifiedHostData = Field(
        frozen=True,
        description="The certified host data loaded from data.json",
    )

    @property
    def is_local(self) -> bool:
        """Check if this host is local. Offline hosts are never local."""
        return False

    def get_name(self) -> HostName:
        """Return the human-readable name of this host from persisted data."""
        return HostName(self.certified_host_data.host_name)

    def get_stop_time(self) -> datetime:
        """Return the host last stop time based on when the host data was last updated."""
        return self.certified_host_data.updated_at

    def get_seconds_since_stopped(self) -> float:
        """Return the number of seconds since this host was stopped, based on updated_at."""
        return (datetime.now(timezone.utc) - self.certified_host_data.updated_at).total_seconds()

    # =========================================================================
    # Certified Data
    # =========================================================================

    def get_certified_data(self) -> CertifiedHostData:
        return self.certified_host_data

    def set_certified_data(self, data: CertifiedHostData) -> None:
        """Save certified data to data.json and notify the provider."""
        assert self.on_updated_host_data is not None, "on_updated_host_data callback is not set"
        # Always stamp updated_at with the current time when writing
        stamped_data = data.model_copy_update(
            to_update(data.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        self.on_updated_host_data(self.id, stamped_data)
