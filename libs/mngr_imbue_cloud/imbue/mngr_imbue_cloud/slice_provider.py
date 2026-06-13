import socket
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.bare_metal import SLICE_VM_DISK_GIB
from imbue.mngr_imbue_cloud.bare_metal import SLICE_VM_MEMORY_MIB
from imbue.mngr_imbue_cloud.bare_metal import allocate_slice_ports
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# region/plan are meaningless for a locally-carved lima VM, but the shared
# VpsDockerProvider finalize path persists them, so use stable placeholders.
_SLICE_REGION: str = "lima"
_SLICE_PLAN: str = "slice"


class SliceVpsDockerProviderConfig(VpsDockerProviderConfig):
    """Config for the slice provider: a VpsDockerProvider whose 'VPS' is a local lima VM."""

    backend: ProviderBackendName = Field(default=ProviderBackendName("imbue_cloud_slice"))
    box_public_address: str = Field(
        default="127.0.0.1",
        description="Address external consumers use to reach slices on this box (recorded on the pool row).",
    )
    slice_vcpus: int = Field(default=2, description="vCPUs per slice VM")
    slice_memory_mib: int = Field(default=SLICE_VM_MEMORY_MIB, description="RAM per slice VM in MiB")
    slice_disk_gib: int = Field(default=SLICE_VM_DISK_GIB, description="btrfs data-disk size per slice VM in GiB")
    slice_port_range_start: int = Field(default=DEFAULT_SLICE_PORT_RANGE_START)
    slice_port_range_end: int = Field(default=DEFAULT_SLICE_PORT_RANGE_END)


def _ensure_btrfs_subvolume_on_outer(outer: OuterHostInterface, subvolume_path: str) -> None:
    """Create a btrfs subvolume at ``subvolume_path`` if it doesn't already exist (idempotent)."""
    script = (
        f"set -e\n"
        f"if ! btrfs subvolume show {subvolume_path} >/dev/null 2>&1; then\n"
        f"    btrfs subvolume create {subvolume_path}\n"
        f"fi\n"
    )
    result = outer.execute_idempotent_command(script, timeout_seconds=60.0)
    if not result.success:
        raise MngrError(f"failed to create btrfs subvolume {subvolume_path}: {result.stderr.strip()}")


class SliceVpsDockerProvider(VpsDockerProvider):
    """A VpsDockerProvider whose 'VPS' is a lima VM we run on a bare-metal box.

    Reuses the shared container bake unchanged; the only differences from a real
    VPS are confined to overridable seams: the outer/inner SSH reach a forwarded
    port on the box (not :22 / :container_ssh_port on a unique IP), and the btrfs
    fs is the lima data disk mounted at ``btrfs_mount_path`` (so we create the
    per-host subvolume directly, with no loopback image).

    Used only for the on-box bake (one slice per ``create_host`` call); slice
    discovery / lease / teardown go through the connector + the DB, not this
    provider, so per-host ports live in instance state for the duration of the bake.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The base ``config`` / ``vps_client`` fields hold these same objects (passed
    # at construction); these narrowly-typed aliases expose the slice-specific
    # knobs and the lima client without re-declaring the base fields (which would
    # be an invariant-override type error -- the pattern OvhProvider uses too).
    slice_config: SliceVpsDockerProviderConfig = Field(frozen=True, description="Slice provider configuration")
    lima_client: LimaSliceVpsClient = Field(frozen=True, description="lima-backed VPS client")

    _current_outer_port: int | None = PrivateAttr(default=None)
    _current_container_port: int | None = PrivateAttr(default=None)

    @property
    def supports_snapshots(self) -> bool:
        return False

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        # Slices have no region/plan flags (the VM is carved locally), so this
        # mirrors MinimalVpsDockerProvider: extract git-depth, pass the rest
        # through as docker build args, and use empty region/plan sentinels
        # (the real values are passed explicitly to create_host_on_existing_vps).
        args = list(build_args or ())
        git_depth, args = extract_git_depth(args)
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            docker_build_args.append(arg)
        return ParsedVpsBuildOptions(
            region=_SLICE_REGION,
            plan=_SLICE_PLAN,
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )

    def _find_two_free_box_ports(self) -> tuple[int, int]:
        """Find two free host ports on the box within the configured slice range.

        Determines which ports in the range are already bound (so concurrently
        baked slices don't collide) and delegates the choice to the pure
        ``allocate_slice_ports``.
        """
        used: set[int] = set()
        for port in range(self.slice_config.slice_port_range_start, self.slice_config.slice_port_range_end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("0.0.0.0", port))
                except OSError:
                    used.add(port)
        return allocate_slice_ports(
            used, self.slice_config.slice_port_range_start, self.slice_config.slice_port_range_end
        )

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
        """Provision a slice VM and bake the shared vps_docker container onto it.

        Mirrors ``VpsDockerProvider.create_host`` but, instead of ordering a VPS
        and uploading an SSH key, carves a lima VM (the LimaSliceVpsClient does
        not support cloud ordering) and reaches it via box-forwarded ports.
        """
        host_id = HostId.generate()
        box = self.slice_config.box_public_address
        logger.info("Creating slice host {} ({}) on box {}", name, host_id, box)

        vm_ssh_port, container_ssh_port = self._find_two_free_box_ports()
        self._current_outer_port = vm_ssh_port
        self._current_container_port = container_ssh_port

        # The provider's VPS keypair authorizes root on the VM; the VPS host
        # keypair is pre-injected as the VM's sshd host key (no first-connect TOFU).
        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair()

        instance_id = VpsInstanceId(slice_lima_instance_name(host_id))
        # Destroy the VM on ANY failure after provisioning (a try/finally + success
        # flag, so we clean up unconditionally without a broad ``except``).
        is_baked = False
        try:
            self.lima_client.provision_slice_vm(
                host_id=host_id,
                vcpus=self.slice_config.slice_vcpus,
                memory_mib=self.slice_config.slice_memory_mib,
                disk_gib=self.slice_config.slice_disk_gib,
                host_dir=str(self.config.btrfs_mount_path),
                root_authorized_public_key=vps_public_key,
                host_private_key_pem=vps_host_key_path.read_text(),
                host_public_key_openssh=vps_host_public_key,
                vm_ssh_host_port=vm_ssh_port,
                container_ssh_host_port=container_ssh_port,
            )
            # Pin the VM's (pre-injected) host key for the forwarded outer port.
            add_host_to_known_hosts(
                known_hosts_path=self._vps_known_hosts_path(),
                hostname=box,
                port=vm_ssh_port,
                public_key=vps_host_public_key,
            )
            wait_for_sshd(hostname=box, port=vm_ssh_port, timeout_seconds=self.config.ssh_connect_timeout)

            with self._make_outer_for_vps_ip(box) as outer:
                host = self.create_host_on_existing_vps(
                    outer=outer,
                    host_id=host_id,
                    name=name,
                    vps_ip=box,
                    vps_instance_id=instance_id,
                    vps_ssh_key_id="",
                    vps_host_public_key=vps_host_public_key,
                    region=_SLICE_REGION,
                    plan=_SLICE_PLAN,
                    image=image,
                    tags=tags,
                    build_args=build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                )
            logger.info("Slice host {} created (instance {})", name, instance_id)
            is_baked = True
            return host
        finally:
            if not is_baked:
                logger.error("Slice host creation failed, destroying VM {}", instance_id)
                try:
                    self.lima_client.destroy_instance(instance_id)
                except MngrError as cleanup_err:
                    logger.warning("Failed to clean up slice VM {}: {}", instance_id, cleanup_err)

    # ------------------------------------------------------------------
    # Per-host-port seam overrides (the bake reaches the VM via box:port)
    # ------------------------------------------------------------------

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        port = self._current_outer_port if self._current_outer_port is not None else 22
        vps_key_path, _pub = self._get_vps_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=port,
            private_key_path=vps_key_path,
            known_hosts_path=self._vps_known_hosts_path(),
            ssh_user="root",
        )
        outer = OuterHost(
            id=HostId.generate(),
            connector=PyinfraConnector(pyinfra_host),
            mngr_ctx=self.mngr_ctx,
        )
        try:
            yield outer
        finally:
            outer.disconnect()

    def _wait_for_container_sshd(self, vps_ip: str) -> None:
        port = (
            self._current_container_port
            if self._current_container_port is not None
            else self.config.container_ssh_port
        )
        wait_for_sshd(hostname=vps_ip, port=port, timeout_seconds=self.config.ssh_connect_timeout)

    def _create_host_object(self, host_id: HostId, host_name: HostName, vps_ip: str) -> Host:
        container_key_path, _container_pub = self._get_container_ssh_keypair()
        _container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        port = (
            self._current_container_port
            if self._current_container_port is not None
            else self.config.container_ssh_port
        )
        # Pin the container sshd's host key for the forwarded external port.
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=vps_ip,
            port=port,
            public_key=container_host_public_key,
        )
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
        )
        host = Host(
            id=host_id,
            host_name=host_name,
            connector=PyinfraConnector(pyinfra_host),
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str) -> None:
        # Same intent as the base (sync data.json into the host volume), but the
        # outer is reached via the forwarded port that _make_outer_for_vps_ip uses.
        super()._on_certified_host_data_updated(host_id, certified_data, vps_ip)

    def _prepare_btrfs_on_outer(self, outer: OuterHostInterface, host_id: HostId):
        # The lima VM already mounts a btrfs fs at btrfs_mount_path (the data
        # disk), so there is no loopback to create -- just make the per-host
        # subvolume the shared bake binds the unified docker volume from.
        subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
        _ensure_btrfs_subvolume_on_outer(outer, str(subvolume_path))
        return subvolume_path


class SliceVpsDockerProviderBackend(ProviderBackendInterface):
    """Backend for the slice provider (lima-VM "VPS" on a bare-metal box).

    Used by the admin bake (``mngr create ...@<host>.imbue_cloud_slice``) which
    runs on the box; the lima client drives limactl locally there.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return ProviderBackendName("imbue_cloud_slice")

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers inside lima VMs ('slices') on a bare-metal box"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return SliceVpsDockerProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "Slice args are passed through to the shared vps_docker bake (e.g. --file=Dockerfile, the build context)."
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run' inside the slice VM."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, SliceVpsDockerProviderConfig):
            raise MngrError(f"Expected SliceVpsDockerProviderConfig, got {type(config).__name__}")
        lima_client = LimaSliceVpsClient()
        return SliceVpsDockerProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=lima_client,
            slice_config=config,
            lima_client=lima_client,
        )
