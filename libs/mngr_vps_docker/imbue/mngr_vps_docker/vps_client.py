from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_vps_docker.errors import VpsDockerError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId


class VpsSnapshotInfo(FrozenModel):
    """Metadata about a VPS-level snapshot."""

    id: VpsSnapshotId = Field(description="Provider-specific snapshot ID")
    description: str = Field(description="Human-readable description")
    created_at: datetime = Field(description="When the snapshot was created")


class VpsSshKeyInfo(FrozenModel):
    """Metadata about an SSH key stored with the VPS provider."""

    id: str = Field(description="Provider-specific SSH key ID")
    name: str = Field(description="Human-readable name")


class VpsClientInterface(MutableModel, ABC):
    """Abstract interface for VPS provider API operations.

    Each method maps to a single API call. The VPS Docker provider layer
    composes these into higher-level operations.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        os_id: int | str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Sequence[str],
    ) -> VpsInstanceId:
        """Provision a new VPS instance. Returns the instance ID."""
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Permanently destroy a VPS instance."""
        ...

    @abstractmethod
    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        """Get the current status of a VPS instance."""
        ...

    @abstractmethod
    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        """Get the main IPv4 address of a VPS instance."""
        ...

    @abstractmethod
    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        """Poll until instance is active and return its IP address."""
        ...

    @abstractmethod
    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        """Create a snapshot of the instance's disk."""
        ...

    @abstractmethod
    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        """Delete a snapshot."""
        ...

    @abstractmethod
    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        """List all snapshots owned by this account."""
        ...

    @abstractmethod
    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Upload an SSH public key. Returns the key ID."""
        ...

    @abstractmethod
    def delete_ssh_key(self, key_id: str) -> None:
        """Delete an SSH key by its ID."""
        ...

    @abstractmethod
    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        """List all SSH keys on the account."""
        ...


class ExternallyManagedVpsClient(VpsClientInterface):
    """A VPS client for VPSes that this process does not own and cannot order.

    Used when a ``VpsDockerProvider`` operates purely on an already-existing,
    externally-provisioned VPS (e.g. ``mngr_imbue_cloud``'s slow path rebuilds
    a container on a leased pool VPS over the SSH access the lease grants).
    Only the container build/teardown methods -- which take an ``outer`` and
    make no VPS-API calls -- are valid in that context; every ordering /
    snapshot / ssh-key operation raises so a wrong call site fails loudly
    instead of silently misbehaving.
    """

    def _unavailable(self, operation: str) -> VpsDockerError:
        return VpsDockerError(
            f"VPS API operation '{operation}' is unavailable: this VPS is externally managed "
            "(e.g. leased from the imbue_cloud pool) and cannot be ordered, destroyed, or "
            "snapshotted by this client."
        )

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        os_id: int | str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Sequence[str],
    ) -> VpsInstanceId:
        raise self._unavailable("create_instance")

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        raise self._unavailable("destroy_instance")

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        raise self._unavailable("get_instance_status")

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        raise self._unavailable("get_instance_ip")

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        raise self._unavailable("wait_for_instance_active")

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        raise self._unavailable("create_snapshot")

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        raise self._unavailable("delete_snapshot")

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        raise self._unavailable("list_snapshots")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise self._unavailable("upload_ssh_key")

    def delete_ssh_key(self, key_id: str) -> None:
        raise self._unavailable("delete_ssh_key")

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        raise self._unavailable("list_ssh_keys")
