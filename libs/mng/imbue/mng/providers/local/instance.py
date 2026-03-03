import json
import shutil
import uuid
from datetime import datetime
from datetime import timezone
from functools import cached_property
from pathlib import Path
from typing import Final
from typing import Mapping
from typing import Sequence
from typing import assert_never

import psutil
from loguru import logger
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State
from pyinfra.api.inventory import Inventory

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import LocalHostNotDestroyableError
from imbue.mng.errors import LocalHostNotStoppableError
from imbue.mng.errors import MngError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import CpuResources
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.providers.local.volume import LocalVolume
from imbue.mng.utils.deps import GIT
from imbue.mng.utils.deps import JQ
from imbue.mng.utils.deps import TMUX
from imbue.mng.utils.file_utils import atomic_write

LOCAL_PROVIDER_SUBDIR: Final[str] = "local"
HOSTS_SUBDIR: Final[str] = "hosts"

# Fixed namespace for deterministic VolumeId derivation from volume directory names.
_LOCAL_VOLUME_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("b7e3d4a1-2f5c-4890-abcd-123456789abc")
HOST_ID_FILENAME: Final[str] = "host_id"
TAGS_FILENAME: Final[str] = "labels.json"


def get_or_create_local_host_id(base_dir: Path) -> HostId:
    """Get the persistent host ID, creating it if it doesn't exist.

    The host_id is stored at {base_dir}/host_id because it identifies
    the local machine, not a particular profile.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    host_id_path = base_dir / HOST_ID_FILENAME

    if host_id_path.exists():
        host_id = HostId(host_id_path.read_text().strip())
        logger.trace("Loaded existing local host id={}", host_id)
        return host_id

    new_host_id = HostId.generate()
    atomic_write(host_id_path, new_host_id)
    logger.debug("Generated new local host id={}", new_host_id)
    return new_host_id


class LocalProviderInstance(BaseProviderInstance):
    """Provider instance for managing the local computer as a host.

    The local provider represents your local machine as a host. It has special
    semantics: the host cannot be stopped or destroyed, and snapshots are not
    supported. The host ID is persistent (generated once and saved to disk).
    """

    def get_host_name(self, style: HostNameStyle) -> HostName:
        return HostName("localhost")

    @property
    def supports_snapshots(self) -> bool:
        return False

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return True

    @property
    def _provider_data_dir(self) -> Path:
        """Get the provider data directory path (not profile-specific, for tags etc)."""
        return self.mng_ctx.config.default_host_dir.expanduser() / "providers" / LOCAL_PROVIDER_SUBDIR

    def _ensure_provider_data_dir(self) -> None:
        """Ensure the provider data directory exists."""
        self._provider_data_dir.mkdir(parents=True, exist_ok=True)

    @cached_property
    def host_id(self) -> HostId:
        base_dir = self.mng_ctx.config.default_host_dir.expanduser()
        return get_or_create_local_host_id(base_dir)

    def _get_tags_path(self) -> Path:
        """Get the path to the tags file."""
        return self._provider_data_dir / TAGS_FILENAME

    def _load_tags(self) -> dict[str, str]:
        """Load tags from the tags file."""
        tags_path = self._get_tags_path()
        if not tags_path.exists():
            return {}

        content = tags_path.read_text()
        if not content.strip():
            return {}

        data = json.loads(content)
        return {item["key"]: item["value"] for item in data}

    def _save_tags(self, tags: Mapping[str, str]) -> None:
        """Save tags to the tags file."""
        self._ensure_provider_data_dir()
        tags_path = self._get_tags_path()
        data = [{"key": key, "value": value} for key, value in tags.items()]
        atomic_write(tags_path, json.dumps(data, indent=2))

    def _create_local_pyinfra_host(self) -> PyinfraHost:
        """Create a pyinfra host for local execution.

        When the host name starts with '@', pyinfra automatically uses the
        LocalConnector, which executes commands locally without SSH.
        The host must be initialized with a State for connection to work.
        """
        names_data = (["@local"], {})
        inventory = Inventory(names_data)
        state = State(inventory=inventory)
        pyinfra_host = inventory.get_host("@local")
        pyinfra_host.init(state)
        return pyinfra_host

    def _create_host(self, name: HostName, tags: Mapping[str, str] | None = None) -> Host:
        """Create a Host object for the local machine."""
        host_id = self.host_id
        pyinfra_host = self._create_local_pyinfra_host()
        connector = PyinfraConnector(pyinfra_host)

        if tags is not None:
            self._save_tags(tags)

        return Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
        )

    # =========================================================================
    # Core Lifecycle Methods
    # =========================================================================

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        """Create (or return) the local host.

        For the local provider, this always returns the same host representing
        the local computer. The name must be "localhost". The image and
        known_hosts parameters are ignored since the local machine uses its
        own configuration.
        """
        TMUX.require()
        GIT.require()
        JQ.require()

        if str(name) != "localhost":
            raise UserInputError(f"Local provider only supports host name 'localhost', got '{name}'")
        with log_span("Creating local host (provider={})", self.name):
            host = self._create_host(name, tags)

            # Record BOOT activity for consistency. In this case it represents when mng first created the local host
            host.record_activity(ActivitySource.BOOT)

        return host

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop the host.

        Always raises LocalHostNotStoppableError because the local computer
        cannot be stopped by mng.
        """
        raise LocalHostNotStoppableError()

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start the host.

        For the local provider, this simply returns the local host since it
        is always running.
        """
        local_host = self._create_host(HostName("localhost"))

        return local_host

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy the host.

        Always raises LocalHostNotDestroyableError because the local computer
        cannot be destroyed by mng.
        """
        raise LocalHostNotDestroyableError()

    def delete_host(self, host: HostInterface) -> None:
        raise Exception("delete_host should not be called for LocalProviderInstance since hosts are never offline")

    def on_connection_error(self, host_id: HostId) -> None:
        pass

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(
        self,
        host: HostId | HostName,
    ) -> Host:
        """Get the local host by ID or name.

        For the local provider, this always returns the same host if the ID
        matches, or raises HostNotFoundError if it doesn't match.
        """
        host_id = self.host_id

        match host:
            case HostId():
                if host != host_id:
                    logger.trace("Failed to find host with id={} (local host id={})", host, host_id)
                    raise HostNotFoundError(host)
            case HostName():
                if str(host) != "localhost":
                    raise HostNotFoundError(host)
            case _ as unreachable:
                assert_never(unreachable)

        return self._create_host(HostName("localhost"))

    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        """List all hosts managed by this provider.

        For the local provider, this always returns a single-element list
        containing the local host.
        """
        hosts = [self._create_host(HostName("localhost"))]
        logger.trace("Listed hosts for local provider {}", self.name)
        return hosts

    # =========================================================================
    # Snapshot Methods
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        """Create a snapshot.

        Always raises SnapshotsNotSupportedError because the local provider
        does not support snapshots.
        """
        raise SnapshotsNotSupportedError(self.name)

    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        """List snapshots for a host.

        Always returns an empty list because the local provider does not
        support snapshots.
        """
        return []

    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        """Delete a snapshot.

        Always raises SnapshotsNotSupportedError because the local provider
        does not support snapshots.
        """
        raise SnapshotsNotSupportedError(self.name)

    # =========================================================================
    # Volume Methods
    # =========================================================================

    @staticmethod
    def _volume_id_for_dir(dir_name: str) -> VolumeId:
        """Create a deterministic VolumeId from a volume directory name.

        Uses UUID5 with a fixed namespace to produce a stable 32-char hex ID
        from any directory name.
        """
        derived = uuid.uuid5(_LOCAL_VOLUME_ID_NAMESPACE, dir_name)
        return VolumeId(f"vol-{derived.hex}")

    @property
    def _hosts_dir(self) -> Path:
        """Get the parent directory containing all host directories."""
        return self.mng_ctx.config.default_host_dir.expanduser() / HOSTS_SUBDIR

    def list_volumes(self) -> list[VolumeInfo]:
        """List all local volumes (subdirectories of ~/.mng/hosts/)."""
        hosts_dir = self._hosts_dir
        if not hosts_dir.is_dir():
            return []
        results: list[VolumeInfo] = []
        for subdir in sorted(hosts_dir.iterdir()):
            if subdir.is_dir():
                stat = subdir.stat()
                host_id = None
                if subdir.name.startswith("host-"):
                    try:
                        host_id = HostId(subdir.name)
                    except ValueError:
                        pass
                results.append(
                    VolumeInfo(
                        volume_id=self._volume_id_for_dir(subdir.name),
                        name=subdir.name,
                        size_bytes=0,
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
                        host_id=host_id,
                    )
                )
        return results

    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a local volume directory."""
        hosts_dir = self._hosts_dir
        if not hosts_dir.is_dir():
            raise MngError(f"Volume {volume_id} not found (no hosts directory)")
        for subdir in hosts_dir.iterdir():
            if subdir.is_dir() and self._volume_id_for_dir(subdir.name) == volume_id:
                shutil.rmtree(subdir)
                logger.debug("Deleted local volume: {}", subdir)
                return
        raise MngError(f"Volume {volume_id} not found")

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the local volume for a host.

        Returns a HostVolume backed by the host_dir (which is ~/.mng/).
        The directory is created if it doesn't exist.
        """
        self.host_dir.mkdir(parents=True, exist_ok=True)
        return HostVolume(volume=LocalVolume(root_path=self.host_dir))

    # =========================================================================
    # Host Mutation Methods
    # =========================================================================

    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        """Get tags for the local host."""
        return self._load_tags()

    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Set tags for the local host."""
        self._save_tags(tags)
        logger.trace("Set {} tag(s) on local host", len(tags))

    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Add tags to the local host."""
        existing_tags = self._load_tags()
        existing_tags.update(tags)
        self._save_tags(existing_tags)
        logger.trace("Added {} tag(s) to local host", len(tags))

    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        """Remove tags by key from the local host."""
        existing_tags = self._load_tags()
        keys_to_remove = set(keys)
        filtered_tags = {k: v for k, v in existing_tags.items() if k not in keys_to_remove}
        self._save_tags(filtered_tags)
        logger.trace("Removed {} tag(s) from local host", len(keys))

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> Host:
        """Rename the local host.

        For the local provider, this is a no-op since the host name is always
        effectively "localhost". Returns the host unchanged.
        """
        return self._create_host(HostName("localhost"))

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> PyinfraHost:
        """Get the pyinfra connector for the local host."""
        return self._create_local_pyinfra_host()

    # =========================================================================
    # Resource Methods (used by Host)
    # =========================================================================

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get resource information for the local host.

        Uses psutil for cross-platform compatibility when available.
        """
        # Get CPU count and frequency
        cpu_count = psutil.cpu_count(logical=True) or 1
        cpu_freq = psutil.cpu_freq() if hasattr(psutil, "cpu_freq") else None
        cpu_freq_ghz = cpu_freq.current / 1000 if cpu_freq else None

        # Get memory in GB
        memory = psutil.virtual_memory()
        memory_gb = memory.total / (1024**3)

        # Get disk space in GB (for root partition)
        try:
            disk = psutil.disk_usage("/")
            disk_gb = disk.total / (1024**3)
        except OSError:
            disk_gb = None

        return HostResources(
            cpu=CpuResources(count=cpu_count, frequency_ghz=cpu_freq_ghz),
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            gpu=None,
        )
