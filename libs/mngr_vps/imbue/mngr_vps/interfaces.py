from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.data_types import AgentEndpoint
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import RealizePlacementContext
from imbue.mngr_vps.data_types import RealizedPlacement
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore


class HostRealizer(MutableModel, ABC):
    """Places an agent on a booted VPS and manages the agent-placement lifecycle.

    The *substrate* (the ``VpsProvider`` and its cloud subclasses) owns the
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

    config: VpsProviderConfig = Field(frozen=True, description="The provider configuration")
    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="The mngr context")
    key_dir: Path = Field(frozen=True, description="Directory holding this provider instance's SSH keys")
    host_dir: Path = Field(frozen=True, description="Base directory for mngr data on the agent host")
    provider_name: ProviderInstanceName = Field(frozen=True, description="Name of the owning provider instance")

    @abstractmethod
    def agent_endpoint(self, vps_ip: str) -> AgentEndpoint:
        """Where (and how) to SSH to the agent placed on ``vps_ip``."""

    @abstractmethod
    def open_host_store(self, outer: OuterHostInterface, host_id: HostId) -> VpsHostStore:
        """Open the host-record / agent-data store for ``host_id`` on the VPS.

        The container realizer resolves a per-host Docker volume; the bare
        realizer points at a plain directory on the VM's root disk. Either way
        the returned store reads/writes the same ``host_state.json`` + ``agents/``
        layout, so the provider treats persistence uniformly.
        """

    @abstractmethod
    def realize_placement(self, outer: OuterHostInterface, ctx: RealizePlacementContext) -> RealizedPlacement:
        """Build and run the agent placement on the booted VPS.

        Does not wait for the agent sshd to be reachable -- the provider owns
        that step so subclasses (e.g. the imbue_cloud slice provider) can wait on
        a dynamically forwarded port.
        """

    @abstractmethod
    def start_activity_watcher(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        """Launch the idle/auto-shutdown activity watcher for the placement."""

    @property
    @abstractmethod
    def idle_shutdown_command(self) -> str:
        """Shell command the host's ``shutdown.sh`` runs when the agent goes idle.

        The container realizer signals the container's PID 1 (``kill -TERM 1``);
        the bare realizer powers the VM off directly (``shutdown -P now``), since
        the agent is the VM's root. On a self-stopping cloud substrate the
        container path can't power off its host from inside a container, so those
        providers override the shutdown handling (sentinel + host-side watcher);
        a bare placement needs no such indirection.
        """

    @abstractmethod
    def host_dir_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of this host's ``host_dir`` tree.

        Container realizer: the per-host btrfs subvolume's ``host_dir``; bare
        realizer: ``host_dir`` under the fixed root-disk store. Used by the
        host-side sentinel path and the host_dir-to-bucket offline sync.
        """

    @property
    @abstractmethod
    def idle_shutdown_stops_host(self) -> bool:
        """Whether ``idle_shutdown_command`` already stops the whole machine.

        True for bare (the command powers the VM off), False for container (it
        only stops the container). Cloud providers whose substrate self-stops use
        this to decide whether the container path needs a host-side sentinel
        watcher to stop the instance -- a bare placement does not.
        """

    @abstractmethod
    def find_host_record(self, outer: OuterHostInterface) -> tuple[HostId, VpsHostRecord] | None:
        """Find the single host on this VPS and read its record, or None if absent.

        The container realizer locates the agent container by its host-id label;
        the bare realizer reads the record straight from the fixed store path.
        """

    @abstractmethod
    def read_live_listing(
        self, outer: OuterHostInterface, host_id: HostId, host_dir: str, prefix: str, window_name: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read live agent data and the running state of the placement.

        Returns ``(agent_data_dicts, is_running)``. Reads agent state from the
        live host_dir (container realizer: via the outer/container script; bare:
        directly on the VM), so in-host-created agents are discovered.
        """

    @abstractmethod
    def is_placement_running(self, outer: OuterHostInterface, handle: PlacementHandle) -> bool:
        """Whether the placement is currently running (container up / VM reachable)."""

    @abstractmethod
    def collect_listing_output(
        self, outer: OuterHostInterface, handle: PlacementHandle, script: str, timeout_seconds: float = 30.0
    ) -> str:
        """Run the inner listing script against the placement and return raw output."""

    @abstractmethod
    def stop_placement(self, outer: OuterHostInterface, handle: PlacementHandle, timeout_seconds: float) -> None:
        """Pause the placement on the machine (container realizer: ``docker stop``; bare: no-op)."""

    @abstractmethod
    def start_placement(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        """Resume the placement on a running machine, without waiting for its sshd."""

    @abstractmethod
    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> None:
        """Remove the placement and its per-host storage (makes no VPS-client calls).

        Best-effort cleanup: attempts every step, records a ``CleanupFailure`` for
        any resource that exists but could not be removed, and raises a
        ``CleanupFailedGroup`` at the end if any were collected (the provider's
        ``destroy_host`` absorbs it into its aggregate). A realizer with no
        per-host storage (bare) tears nothing down and raises nothing.
        """


class SnapshotCapableRealizer(HostRealizer, ABC):
    """A ``HostRealizer`` that can snapshot its placement.

    Snapshot support is a structural fact: only realizers that subclass this can
    create and delete placement snapshots. A plain ``HostRealizer`` (e.g. the
    bare realizer) has no snapshot methods at all, so the provider gates snapshot
    operations once at its boundary rather than reaching into a realizer that
    would only raise.
    """

    @abstractmethod
    def snapshot_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> SnapshotId:
        """Create a placement snapshot and return its id."""

    @abstractmethod
    def delete_snapshot_placement(self, outer: OuterHostInterface, snapshot_id: SnapshotId) -> None:
        """Delete a placement snapshot."""
