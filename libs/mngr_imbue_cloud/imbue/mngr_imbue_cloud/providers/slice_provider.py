from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.primitives import VpsInstanceId

# region/plan are meaningless for a locally-carved lima VM, but the shared
# VpsProvider finalize path persists them, so use stable placeholders.
# Region falls back to this only if the owning bare-metal server's region is
# unknown; the slice bake always passes the real region via ``slice_region``.
_FALLBACK_SLICE_REGION: str = "lima"
_SLICE_PLAN: str = "slice"


class SliceVpsDockerProviderConfig(VpsProviderConfig):
    """Config for the slice provider: a VpsProvider whose 'VPS' is a local lima VM."""

    backend: ProviderBackendName = Field(default=ProviderBackendName("imbue_cloud_slice"))
    box_public_address: str = Field(
        default="127.0.0.1",
        description="Address external consumers use to reach slices on this box (also where the bake SSHes to carve).",
    )
    box_ssh_user: str = Field(
        default="limahost",
        description="Dedicated non-root lima user on the box; the bake SSHes in as this user to run limactl.",
    )
    pool_private_key_path: str | None = Field(
        default=None,
        description=(
            "Path (on the machine running the bake) to the pool management private key used to SSH the box "
            "for the limactl carve. Set by ``admin pool create --backend slice`` from POOL_SSH_PRIVATE_KEY."
        ),
    )
    slice_base_image_url: str | None = Field(
        default=None,
        description=(
            "Guest OS image the slice VM boots from. Defaults to the box-staged image "
            "(``file://`` under the lima user's home, placed there once at ``server prep``) so bakes never "
            "depend on the Debian mirror. Set to None only to fall back to mngr_lima's default (mirror) image."
        ),
    )
    pool_authorized_public_key: str | None = Field(
        default=None,
        description=(
            "Pool management public key to authorize for the slice's VM root and inner container, so the "
            "connector can inject the leasing user's key at lease time and reach the VM at release time. "
            "Set by the bake (``admin pool create --backend slice``) from POOL_SSH_PRIVATE_KEY."
        ),
    )
    box_host_public_key: str | None = Field(
        default=None,
        description=(
            "The bare-metal box's sshd host public key, pinned by the lima slice client for strict "
            "host-key checking (no trust-on-first-use). Set by the bake from the box's bare_metal_servers row."
        ),
    )
    slice_region: str | None = Field(
        default=None,
        description="Region recorded on the slice's host record (the owning bare-metal server's region).",
    )
    slice_env_name: str | None = Field(
        default=None,
        description=(
            "Owning environment name stamped into the slice's lima instance + disk names "
            "(mngr-slice-<env>-<host-hex>), so a shared box can attribute the slice to an env and "
            "reconciliation scopes itself to one env. None produces legacy un-stamped names."
        ),
    )
    # Carving knobs: deliberately have NO defaults (None). They vary per box (a
    # function of its RAM/cores/disk + the chosen per-slice RAM and overcommit) and
    # are computed by ``admin pool create --backend slice`` and passed in per bake via
    # ``-S`` overrides. ``provision_slice_vm`` raises if any is unset when carving.
    slice_vcpus: int | None = Field(default=None, description="vCPUs per slice VM (no default; set per box)")
    slice_memory_mib: int | None = Field(default=None, description="RAM per slice VM in MiB (no default; set per box)")
    slice_disk_gib: int | None = Field(
        default=None, description="btrfs data-disk size per slice VM in GiB (no default; set per box)"
    )
    slice_slot_count: int | None = Field(
        default=None,
        description=(
            "The box's total slice slot count (no default; set per box). The on-box reservation refuses to "
            "carve once the box already holds this many slices -- the cross-env over-allocation guard."
        ),
    )
    slice_port_range_start: int | None = Field(default=None, description="Box host-port range start (no default)")
    slice_port_range_end: int | None = Field(default=None, description="Box host-port range end (no default)")


class SliceVpsDockerProvider(VpsProvider):
    """A VpsProvider whose 'VPS' is a lima VM we run on a bare-metal box.

    The bake runs from wherever ``mngr create`` is invoked (the operator's laptop,
    like an OVH bake): ``create_host`` carves the VM by driving limactl over SSH on
    the box (via :class:`LimaSliceVpsClient`), then reaches the VM's box-forwarded
    ports to build the container. Reuses the shared container bake unchanged; the
    only differences from a real VPS are confined to overridable seams: the
    outer/inner SSH reach a forwarded port on the box (not :22 / :container_ssh_port
    on a unique IP), and the btrfs fs is the lima data disk mounted at
    ``btrfs_mount_path`` (so we create the per-host subvolume directly, with no
    loopback image).

    One slice per ``create_host`` call; slice discovery / lease / teardown go
    through the connector + the DB, not this provider, so per-host ports live in
    instance state for the duration of the bake.
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
    # Per-host VM-root (outer) forwarded port, recorded at bake time so
    # ``get_outer_ssh_port`` can surface it through ``mngr create --format json``
    # (the row's ``ssh_port``; the agent connection uses the container port).
    _outer_port_by_host_id: dict[HostId, int] = PrivateAttr(default_factory=dict)

    @property
    def supports_snapshots(self) -> bool:
        return False

    def get_outer_ssh_port(self, host_id: HostId) -> int | None:
        return self._outer_port_by_host_id.get(host_id)

    def set_forwarded_ports(self, *, outer_port: int, container_port: int) -> None:
        """Point the per-host-port seams at known box-forwarded ports.

        Used when rebuilding the container on an *already-leased* slice (the
        imbue_cloud slow path): the VM + its lima port-forwards already exist, so
        instead of allocating ports the provider must reach the lease's recorded
        VM-root (outer) and inner-container forwarded ports.
        """
        self._current_outer_port = outer_port
        self._current_container_port = container_port

    def _resolved_region(self) -> str:
        """The owning bare-metal server's region, or a fallback if unknown."""
        return self.slice_config.slice_region or _FALLBACK_SLICE_REGION

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        # Slices have no region/plan flags (the VM is carved locally), so this
        # mirrors MinimalVpsProvider: extract git-depth, pass the rest
        # through as docker build args. Region is the owning server's region.
        args = list(build_args or ())
        git_depth, args = extract_git_depth(args)
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            docker_build_args.append(arg)
        return ParsedVpsBuildOptions(
            region=self._resolved_region(),
            plan=_SLICE_PLAN,
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
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

        Mirrors ``VpsProvider.create_host`` but, instead of ordering a VPS
        and uploading an SSH key, carves a lima VM (the LimaSliceVpsClient does
        not support cloud ordering) and reaches it via box-forwarded ports.
        """
        host_id = HostId.generate()
        box = self.slice_config.box_public_address
        env_name = self.slice_config.slice_env_name
        logger.info("Creating slice host {} ({}) on box {} (env={})", name, host_id, box, env_name)

        # The provider's VPS keypair authorizes root on the VM; this host's unique
        # VPS host keypair is pre-injected as the VM's sshd host key (no
        # first-connect TOFU).
        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair(host_id)

        instance_id = VpsInstanceId(slice_lima_instance_name(host_id, env_name))
        # Carving knobs have no defaults; they must have been set (per box) via -S.
        vcpus = self.slice_config.slice_vcpus
        memory_mib = self.slice_config.slice_memory_mib
        disk_gib = self.slice_config.slice_disk_gib
        slot_count = self.slice_config.slice_slot_count
        port_range_start = self.slice_config.slice_port_range_start
        port_range_end = self.slice_config.slice_port_range_end
        if (
            vcpus is None
            or memory_mib is None
            or disk_gib is None
            or slot_count is None
            or port_range_start is None
            or port_range_end is None
        ):
            raise MngrError(
                "slice_vcpus / slice_memory_mib / slice_disk_gib / slice_slot_count / slice_port_range_* must all "
                "be set to carve a slice (they are computed per box by `admin pool create --backend slice`)"
            )
        region = self._resolved_region()
        # The pool management key (when configured) is authorized on both the VM
        # root and the inner container so the connector can inject the leasing
        # user's key at lease time and reach the VM at release time.
        pool_key = self.slice_config.pool_authorized_public_key
        extra_root_keys = (pool_key,) if pool_key else ()
        effective_authorized_keys = [pool_key, *(authorized_keys or ())] if pool_key else list(authorized_keys or ())
        # Destroy the VM on ANY failure after provisioning (a try/finally + success
        # flag, so we clean up unconditionally without a broad ``except``).
        is_baked = False
        try:
            # Reserve the box slot + host ports (under the box lock) and boot the
            # env-stamped VM. The ports are chosen on the box, so they come back here.
            provision_result = self.lima_client.provision_slice_vm(
                host_id=host_id,
                env_name=env_name,
                vcpus=vcpus,
                memory_mib=memory_mib,
                disk_gib=disk_gib,
                host_dir=str(self.config.btrfs_mount_path),
                root_authorized_public_key=vps_public_key,
                host_private_key_pem=vps_host_key_path.read_text(),
                host_public_key_openssh=vps_host_public_key,
                boot_disk_gib=SLICE_BOOT_DISK_GIB,
                slot_count=slot_count,
                port_range_start=port_range_start,
                port_range_end=port_range_end,
                extra_root_authorized_keys=extra_root_keys,
            )
            vm_ssh_port = provision_result.vm_ssh_host_port
            container_ssh_port = provision_result.container_ssh_host_port
            self._current_outer_port = vm_ssh_port
            self._current_container_port = container_ssh_port
            self._outer_port_by_host_id[host_id] = vm_ssh_port
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
                    region=region,
                    plan=_SLICE_PLAN,
                    image=image,
                    tags=tags,
                    build_args=build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=effective_authorized_keys,
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

    def _wait_for_container_sshd(self, vps_ip: str, realizer: HostRealizer | None = None) -> None:
        # imbue_cloud is container-only (it rejects bare), and the agent sshd is
        # reached on a dynamically forwarded port, so the realizer is unused here.
        del realizer
        port = (
            self._current_container_port
            if self._current_container_port is not None
            else self.config.container_ssh_port
        )
        wait_for_sshd(hostname=vps_ip, port=port, timeout_seconds=self.config.ssh_connect_timeout)

    def _create_host_object(self, host_id: HostId, host_name: HostName, vps_ip: str, realizer: HostRealizer) -> Host:
        container_key_path, _container_pub = self._get_container_ssh_keypair()
        _container_host_key_path, container_host_public_key = self._get_container_host_keypair(host_id)
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
                callback_host_id, certified_data, vps_ip, realizer
            ),
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def _on_certified_host_data_updated(
        self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str, realizer: HostRealizer
    ) -> None:
        # Same intent as the base (sync data.json into the host volume), but the
        # outer is reached via the forwarded port that _make_outer_for_vps_ip uses.
        super()._on_certified_host_data_updated(host_id, certified_data, vps_ip, realizer)
