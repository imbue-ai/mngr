from abc import ABC
from abc import abstractmethod
from pathlib import Path

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.data_types import AgentEndpoint
from imbue.mngr_vps_docker.data_types import RealizePlacementContext
from imbue.mngr_vps_docker.data_types import RealizedPlacement
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord


class HostRealizer(MutableModel, ABC):
    """Places an agent on a booted VPS and manages the agent-placement lifecycle.

    The *substrate* (the ``VpsDockerProvider`` and its cloud subclasses) owns the
    machine: provisioning, boot, instance stop/start/destroy, the host record,
    and discovery. The *realizer* owns how the agent sits on that machine and the
    placement lifecycle: building/running the placement, where to SSH to the
    agent, pausing/resuming/removing the placement, and snapshots. The provider
    selects one realizer from ``config.isolation`` and its default method
    implementations delegate the placement concerns here.

    A realizer never opens its own outer connection; the provider passes in an
    ``OuterHostInterface`` (already targeting the VPS) so all SSH transport and
    host-key policy stay owned by the substrate.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VpsDockerProviderConfig = Field(frozen=True, description="The provider configuration")
    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="The mngr context")
    key_dir: Path = Field(frozen=True, description="Directory holding this provider instance's SSH keys")
    host_dir: Path = Field(frozen=True, description="Base directory for mngr data on the agent host")
    provider_name: ProviderInstanceName = Field(frozen=True, description="Name of the owning provider instance")

    @property
    @abstractmethod
    def supports_snapshots(self) -> bool:
        """Whether this realizer can snapshot a placement."""

    @abstractmethod
    def agent_endpoint(self, vps_ip: str) -> AgentEndpoint:
        """Where (and how) to SSH to the agent placed on ``vps_ip``."""

    @abstractmethod
    def realize_placement(self, outer: OuterHostInterface, ctx: RealizePlacementContext) -> RealizedPlacement:
        """Build and run the agent placement on the booted VPS.

        Does not wait for the agent sshd to be reachable -- the provider owns
        that step so subclasses (e.g. the imbue_cloud slice provider) can wait on
        a dynamically forwarded port.
        """

    @abstractmethod
    def start_activity_watcher(self, outer: OuterHostInterface, container_name: str | None) -> None:
        """Launch the idle/auto-shutdown activity watcher for the placement."""

    @abstractmethod
    def stop_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord, timeout_seconds: float) -> None:
        """Pause the placement on the machine (container realizer: ``docker stop``; bare: no-op)."""

    @abstractmethod
    def start_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord) -> None:
        """Resume the placement on a running machine, without waiting for its sshd."""

    @abstractmethod
    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId, record: VpsDockerHostRecord) -> None:
        """Remove the placement and its per-host storage (makes no VPS-client calls)."""

    @abstractmethod
    def snapshot_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord) -> SnapshotId:
        """Create a placement snapshot and return its id; raise if unsupported."""

    @abstractmethod
    def delete_snapshot_placement(self, outer: OuterHostInterface, snapshot_id: SnapshotId) -> None:
        """Delete a placement snapshot."""
