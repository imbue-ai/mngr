from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
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
        os_id: int,
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
