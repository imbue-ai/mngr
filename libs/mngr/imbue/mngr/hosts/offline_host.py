from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import DuplicateAgentNameError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName


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


@pure
def apply_rename_to_agent_data(
    data: Mapping[str, Any],
    new_name: AgentName,
    labels_to_merge: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Return a new agent data dict with the name updated and labels merged.

    Shared by online and offline rename paths so the read-modify rule for
    ``data.json`` is identical regardless of where the data is persisted.
    Existing label keys are overwritten by ``labels_to_merge``.
    """
    updated = dict(data)
    updated["name"] = str(new_name)
    if labels_to_merge:
        current_labels = dict(updated.get("labels") or {})
        updated["labels"] = {**current_labels, **dict(labels_to_merge)}
    return updated


class BaseHost(HostInterface):
    """Base for host implementations (shared between offline and online hosts)."""

    provider_instance: ProviderInstanceInterface = Field(
        frozen=True, description="The provider instance managing this host"
    )
    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="The mngr context")
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

    def _check_rename_conflict(self, agent_id: AgentId, new_name: AgentName) -> None:
        """Raise DuplicateAgentNameError if another agent on this host uses ``new_name``.

        Uses ``discover_agents()`` so the check is identical for online and
        offline hosts (online overrides discovery to read straight from the
        host filesystem; offline reads from the provider's persisted data).
        """
        for existing in self.discover_agents():
            if existing.agent_name == new_name and existing.agent_id != agent_id:
                raise DuplicateAgentNameError(new_name, existing.agent_id)

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================
    def get_state(self) -> HostState:
        """Get the current state of the host.

        Delegates to derive_offline_host_state() which contains the canonical
        state-derivation logic shared with provider discovery code.
        """
        return derive_offline_host_state(
            certified_data=self.get_certified_data(),
            supports_shutdown_hosts=self.provider_instance.supports_shutdown_hosts,
            supports_snapshots=self.provider_instance.supports_snapshots,
            has_snapshots=len(self.get_snapshots()) > 0,
        )

    def get_failure_reason(self) -> str | None:
        """Get the failure reason if this host failed during creation."""
        return self.get_certified_data().failure_reason

    def get_build_log(self) -> str | None:
        """Get the build log if this host failed during creation."""
        return self.get_certified_data().build_log


def derive_offline_host_state(
    certified_data: CertifiedHostData,
    supports_shutdown_hosts: bool,
    supports_snapshots: bool,
    has_snapshots: bool,
) -> HostState:
    """Determine the lifecycle state of an offline host from its certified data.

    This is the canonical logic for deriving host state without connecting to the
    host. Both OfflineHost.get_state() and provider discovery code should use this
    to avoid duplicating the state-derivation rules.

    has_snapshots should reflect the most authoritative source available to the
    caller: provider.list_snapshots() when accessible (OfflineHost.get_state),
    or certified_data.snapshots when not (discovery code).
    """
    if certified_data.failure_reason is not None:
        return HostState.FAILED

    stop_reason = certified_data.stop_reason

    if supports_shutdown_hosts:
        # Provider supports controlled shutdown, so stop_reason is authoritative.
        # None means the host crashed (no controlled shutdown recorded).
        if stop_reason is None:
            return HostState.CRASHED
        return HostState(stop_reason)

    # Provider does not support shutdown (e.g. Modal). stop_reason may be
    # unset, so fall through to snapshot-based state derivation.
    if not supports_snapshots:
        return HostState.DESTROYED

    if not has_snapshots:
        return HostState.DESTROYED

    # Has snapshots -- use stop_reason if set, otherwise CRASHED.
    if stop_reason is None:
        return HostState.CRASHED
    return HostState(stop_reason)


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

    # =========================================================================
    # Agent Operations
    # =========================================================================

    def rename_agent(
        self,
        agent_ref: DiscoveredAgent,
        new_name: AgentName,
        labels_to_merge: Mapping[str, str] | None = None,
    ) -> DiscoveredAgent:
        """Rename an agent on this offline host by editing the provider's persisted data.

        Tmux sessions and on-host env files are not touched -- the host is
        offline, so no tmux session exists, and the env file (which contains
        ``MNGR_AGENT_NAME``) is regenerated when the agent is next
        provisioned. ``data.json`` is the source of truth for the agent name.

        Raises :class:`AgentNotFoundOnHostError` if the agent is missing
        from the persisted records.
        """
        with log_span(
            "Renaming offline agent",
            agent_id=str(agent_ref.agent_id),
            old_name=str(agent_ref.agent_name),
            new_name=str(new_name),
        ):
            self._check_rename_conflict(agent_ref.agent_id, new_name)
            target_id_str = str(agent_ref.agent_id)
            for record in self.provider_instance.list_persisted_agent_data_for_host(self.id):
                if record.get("id") != target_id_str:
                    continue
                updated = apply_rename_to_agent_data(record, new_name, labels_to_merge)
                self.provider_instance.persist_agent_data(self.id, updated)
                return DiscoveredAgent(
                    host_id=self.id,
                    agent_id=agent_ref.agent_id,
                    agent_name=new_name,
                    provider_name=self.provider_instance.name,
                    certified_data=updated,
                )
            raise AgentNotFoundOnHostError(agent_ref.agent_id, self.id)


class OfflineHostWithVolume(OfflineHost, HostFileReadInterface):
    """An offline host whose persisted storage volume is still readable.

    A plain :class:`OfflineHost` exposes only last-known metadata. When the
    provider also surfaces a persistent volume for the host, the host's files
    survive it being stopped, so we can still *read* them even though no
    SSH/command execution is possible. This class implements
    :class:`~imbue.mngr.interfaces.host.HostFileReadInterface` on top of that
    volume, so callers that only need to read files (session preservation,
    ``mngr file get``/``list``, event/transcript readers, map-reduce output
    pulling) can treat a stopped-but-volume-backed host uniformly with an online
    one.

    The volume is resolved **lazily** -- on first file access, not at
    construction. This keeps host *discovery* cheap: ``to_offline_host`` is
    called in discovery fallbacks, and for some providers (e.g. Modal) resolving
    the volume requires a network probe. Deferring the probe to the first read
    means listing/discovering stopped hosts never pays for it; only code that
    actually reads files does. The resolved volume (or its absence) is cached.
    If no volume is available, reads behave as "nothing there": ``path_exists``
    is False, ``list_directory`` is empty, and ``read_file`` raises
    ``FileNotFoundError``.

    Paths are addressed exactly as on an online host -- absolute paths under
    ``host_dir``. The host volume is rooted at ``host_dir`` (it is the host's
    persisted state directory), so an absolute path is translated to a
    volume-relative one by stripping the ``host_dir`` prefix.
    """

    _resolved_volume: Volume | None = PrivateAttr(default=None)
    _is_volume_resolved: bool = PrivateAttr(default=False)

    @classmethod
    def from_offline_host(cls, host: OfflineHost, host_volume: Volume | None = None) -> "OfflineHostWithVolume":
        """Build a readable offline host from a plain OfflineHost.

        ``host_volume`` may be passed to seed the volume eagerly (e.g. in tests
        or when it is already known); otherwise it is resolved lazily from the
        provider on first read.
        """
        readable = cls(
            id=host.id,
            certified_host_data=host.certified_host_data,
            provider_instance=host.provider_instance,
            mngr_ctx=host.mngr_ctx,
            on_updated_host_data=host.on_updated_host_data,
        )
        if host_volume is not None:
            readable._resolved_volume = host_volume
            readable._is_volume_resolved = True
        return readable

    def _volume(self) -> Volume | None:
        """Return the host's volume, resolving (and caching) it on first use."""
        if not self._is_volume_resolved:
            try:
                host_volume = self.provider_instance.get_volume_for_host(self.id)
            except (MngrError, OSError) as e:
                logger.trace("Failed to resolve volume for offline host {}: {}", self.id, e)
                host_volume = None
            self._resolved_volume = host_volume.volume if host_volume is not None else None
            self._is_volume_resolved = True
        return self._resolved_volume

    def _to_volume_path(self, path: Path) -> str:
        """Translate an absolute path under host_dir to a volume-relative path."""
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.host_dir)
        except ValueError as e:
            raise MngrError(
                f"Path {candidate} is not under host_dir {self.host_dir}; "
                "OfflineHostWithVolume can only read files within the host's volume."
            ) from e
        text = str(relative)
        return "" if text == "." else text

    def read_file(self, path: Path) -> bytes:
        """Read a file from the host volume."""
        volume = self._volume()
        if volume is None:
            raise FileNotFoundError(f"No readable volume for offline host {self.id}; cannot read {path}")
        return volume.read_file(self._to_volume_path(path))

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        """Read a file from the host volume and decode it."""
        return self.read_file(path).decode(encoding)

    def path_exists(self, path: Path) -> bool:
        """Whether a path exists on the host volume."""
        volume = self._volume()
        if volume is None:
            return False
        return volume.path_exists(self._to_volume_path(path))

    def get_file_mtime(self, path: Path) -> datetime | None:
        """Return the modification time of a file via its parent directory listing."""
        target = str(Path(path))
        for entry in self.list_directory(Path(path).parent):
            if entry.path == target:
                return datetime.fromtimestamp(entry.mtime, tz=timezone.utc)
        return None

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        """List entries under ``path`` on the host volume.

        Returns VolumeFiles with absolute ``path`` values under ``host_dir`` so
        the addressing matches online hosts.
        """
        volume = self._volume()
        if volume is None:
            return []
        return self._list_volume_dir(volume, self._to_volume_path(path), recursive)

    def _list_volume_dir(self, volume: Volume, volume_path: str, recursive: bool) -> list[VolumeFile]:
        try:
            raw_entries = volume.listdir(volume_path)
        except (MngrError, OSError) as e:
            logger.trace("Failed to list volume directory '{}': {}", volume_path, e)
            return []
        results: list[VolumeFile] = []
        for entry in raw_entries:
            results.append(
                VolumeFile(
                    path=str(self.host_dir / entry.path),
                    file_type=entry.file_type,
                    mtime=entry.mtime,
                    size=entry.size,
                )
            )
            if recursive and entry.file_type == VolumeFileType.DIRECTORY:
                results.extend(self._list_volume_dir(volume, entry.path, recursive))
        return results


def make_readable_offline_host(host: OfflineHost) -> OfflineHost:
    """Return a readable form of an offline host.

    Providers call this from ``to_offline_host`` so the returned offline host
    implements :class:`~imbue.mngr.interfaces.host.HostFileReadInterface`,
    reading the host's persisted volume when one is available. The volume is
    resolved lazily on first read (see :class:`OfflineHostWithVolume`), so this
    call is cheap and adds no per-host probe to host discovery.
    """
    return OfflineHostWithVolume.from_offline_host(host)
