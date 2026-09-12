import base64
import shlex
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
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
from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.slices.bare_metal import box_image_cache_dir_for_generation
from imbue.mngr_imbue_cloud.slices.bare_metal import build_slice_container_memory_start_args
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_instance_name
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.box_image_cache import TransferKey
from imbue.mngr_imbue_cloud.slices.box_image_cache import WAIT_FOR_TAR_TIMEOUT_SECONDS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_CONTAINER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PUBLIC_KEY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_ROOT_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_trust_files
from imbue.mngr_imbue_cloud.slices.ssh_box_image_cache import SshBoxImageCache
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.container_setup import build_image_on_outer_from_build_args
from imbue.mngr_vps.container_setup import run_docker
from imbue.mngr_vps.data_types import ContainerFile
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.primitives import VpsInstanceId

# region/plan are meaningless for a locally-carved lima VM, but the shared
# VpsProvider finalize path persists them, so use stable placeholders.
# Region falls back to this only if the owning bare-metal server's region is
# unknown; the slice bake always passes the real region via ``slice_region``.
_FALLBACK_SLICE_REGION: str = "lima"
_SLICE_PLAN: str = "slice"

# Conservative free-disk requirement (bytes) checked on the box before saving the
# DEFAULT_WORKSPACE_TEMPLATE image tar -- the box boot disk is shared, so fail early rather than fill it.
_ESTIMATED_DEFAULT_WORKSPACE_TEMPLATE_IMAGE_BYTES: Final[int] = 15 * 1024**3
# How many check-tar/contend-lock/wait rounds a slice runs before giving up on the
# box image cache. Each no-progress round burns a full WAIT_FOR_TAR_TIMEOUT_SECONDS
# (a live builder that never publishes); dead-seeder rounds recycle in seconds, so
# this mostly bounds the pathological wedged-builder case. The enclosing
# ``mngr create`` timeout bounds the total wall clock well below the worst case.
_IMAGE_CACHE_WAIT_ROUNDS: Final[int] = 4
# Generous cap for the seeding slice's base DEFAULT_WORKSPACE_TEMPLATE image build (the inner create budget
# is 45 min; the build is the long pole, the Playwright derive + save the rest).
_SEED_BASE_BUILD_TIMEOUT_SECONDS: Final[float] = 1800.0
# The Playwright-derived image RUNs a chromium download + apt; retry transient
# failures a few times before hard-failing the seed.
_PLAYWRIGHT_BUILD_ATTEMPTS: Final[int] = 3
_PLAYWRIGHT_BUILD_TIMEOUT_SECONDS: Final[float] = 900.0
_PLAYWRIGHT_CTX_DIR: Final[str] = "/tmp/default-workspace-template-playwright-ctx"
_BUILDER_PRUNE_TIMEOUT_SECONDS: Final[float] = 120.0
# The DEFAULT_WORKSPACE_TEMPLATE Dockerfile relocates the built workspace here (off the /mngr volume mount)
# before first boot; the Playwright derive runs ``uv run`` from it. This is a DEFAULT_WORKSPACE_TEMPLATE image
# contract -- if DEFAULT_WORKSPACE_TEMPLATE moves it, the derive's guard fails fast with a clear message.
_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR: Final[str] = "/docker_build_code"
# The env-converge browser unit's satisfied condition: baking the Fortress
# engine (and its Chromium apt libs) into the seeded image makes the unit a
# fast no-op on every loaded slice -- there are no marker files anymore, the
# unit checks the real installed state. The path is relative to the workspace
# repo root (which the DEFAULT_WORKSPACE_TEMPLATE Dockerfile relocates to
# /docker_build_code) and must match where env-converge finds its units
# (``<workspace>/system/scripts/env.d/``).
_ENV_D_BROWSER_UNIT: Final[str] = "system/scripts/env.d/1000-playwright-fortress.sh"

# How long to wait for a freshly-carved gen-2 guest's first-boot cloud-init to
# finish before touching the VM. Its sshd answers well before the first boot
# completes (package installs, the data-disk format/mount, docker bring-up),
# and outer provisioning racing it collides on the dpkg lock and can observe a
# half-provisioned guest.
_GUEST_FIRST_BOOT_TIMEOUT_SECONDS: Final[float] = 600.0

# Where the slice VM's root sshd reads its authorized keys; the bake's
# ephemeral box-to-VM transfer key is authorized and removed here.
_VM_ROOT_SSH_DIR: Final[str] = "/root/.ssh"
_VM_ROOT_AUTHORIZED_KEYS_PATH: Final[str] = f"{_VM_ROOT_SSH_DIR}/authorized_keys"


def wait_for_guest_cloud_init_to_finish(outer: OuterHostInterface) -> None:
    """Block until the guest's cloud-init reaches a terminal state.

    Purely a serialization barrier: a degraded terminal state is logged and
    tolerated (the following provisioning steps validate real functionality
    and fail loudly themselves), but proceeding mid-boot is never safe.
    """
    result = outer.execute_idempotent_command(
        "cloud-init status --wait", timeout_seconds=_GUEST_FIRST_BOOT_TIMEOUT_SECONDS
    )
    if not result.success:
        logger.warning(
            "cloud-init reported a non-success terminal state on the slice guest (continuing): {} {}",
            result.stdout.strip(),
            result.stderr.strip(),
        )


@pure
def container_ca_trust_files(trusted_user_ca_public_key: str) -> tuple[ContainerFile, ...]:
    """The container's sshd trust files: the tier CA plus a root principals file accepting the container principal."""
    return tuple(
        ContainerFile(path=trust_file.path, content=trust_file.content, mode=trust_file.mode)
        for trust_file in ssh_ca_trust_files(
            trusted_user_ca_public_key, {SSH_CA_ROOT_USER: SSH_CA_PRINCIPAL_CONTAINER}
        )
    )


class SliceSshAuthority(FrozenModel):
    """What a carve authorizes for management SSH on the VM root and inner container, by box generation."""

    vm_trusted_user_ca_public_key: str | None = Field(
        description="Passed to provision_slice_vm: the tier CA on gen-2, None on gen-1 (no CA trust)"
    )
    extra_root_authorized_keys: tuple[str, ...] = Field(
        description="Gen-1 only: the pool management public key, so it is also authorized on the VM root"
    )
    container_ssh_config_files: tuple[ContainerFile, ...] = Field(
        description="Gen-2 only: the container's CA trust files (empty on gen-1, which authorizes a static key)"
    )


@pure
def resolve_slice_ssh_authority(
    *,
    is_gen2: bool,
    trusted_user_ca_public_key: str | None,
    pool_authorized_public_key: str | None,
) -> SliceSshAuthority:
    """What a carve authorizes for management SSH: the tier CA on gen-2, the static pool key on gen-1.

    Raises :class:`MngrError` when a gen-2 carve has no CA public key set (the
    tier's Vault SSH CA has not been brought up and committed, and a gen-2 box
    has no other management-access path).
    """
    if is_gen2 and trusted_user_ca_public_key is None:
        raise MngrError(
            "trusted_user_ca_public_key must be set to carve a gen-2 slice (the tier's SSH CA public key, "
            "committed in its deploy.toml [ssh_ca] block and passed by the operator pool bake)"
        )
    pool_key = None if is_gen2 else pool_authorized_public_key
    return SliceSshAuthority(
        vm_trusted_user_ca_public_key=trusted_user_ca_public_key if is_gen2 else None,
        extra_root_authorized_keys=(pool_key,) if pool_key else (),
        container_ssh_config_files=(
            container_ca_trust_files(trusted_user_ca_public_key)
            if is_gen2 and trusted_user_ca_public_key is not None
            else ()
        ),
    )


def read_container_ca_trust_files_from_vm(outer: OuterHostInterface) -> tuple[ContainerFile, ...]:
    """The container CA trust files rendered from the CA the slice VM itself trusts (empty when it trusts none).

    A rebuilt agent container must keep trusting the tier CA the bake gave
    it, and the VM is the on-machine source of truth for that key.
    """
    result = outer.execute_idempotent_command(
        f"if [ -e {SSH_CA_PUBLIC_KEY_PATH} ]; then cat {SSH_CA_PUBLIC_KEY_PATH}; fi", timeout_seconds=30.0
    )
    if not result.success:
        raise MngrError(f"could not read the slice VM's trusted SSH CA ({SSH_CA_PUBLIC_KEY_PATH}): {result.stderr}")
    ca_public_key = result.stdout.strip()
    if not ca_public_key:
        logger.warning(
            "The slice VM trusts no SSH CA ({} is absent: it was carved before the tier's SSH CA rollout); "
            "the rebuilt agent container will accept no management certificate either",
            SSH_CA_PUBLIC_KEY_PATH,
        )
        return ()
    return container_ca_trust_files(ca_public_key)


class SliceVpsDockerProviderConfig(VpsProviderConfig):
    """Config for the slice provider: a VpsProvider whose 'VPS' is a local lima VM."""

    backend: ProviderBackendName = Field(default=ProviderBackendName("imbue_cloud_slice"))
    box_public_address: str = Field(
        default="127.0.0.1",
        description="Address external consumers use to reach slices' forwarded ports on this box.",
    )
    box_management_address: str | None = Field(
        default=None,
        description=(
            "Address the bake dials for the box's management SSH (the carve). The operator pool bake "
            "threads its resolved dial here -- a local userspace-tunnel forward or the overlay address "
            "once the box's :22 lockdown is live. None falls back to box_public_address."
        ),
    )
    box_management_ssh_port: int | None = Field(
        default=None,
        description="Port for the box management SSH dial (a tunnel's local forward port). None means 22.",
    )
    box_ssh_user: str = Field(
        default=GEN2_SLICE_SERVICE_USER,
        description=(
            "Dedicated non-root service user on the box; the bake SSHes in as this user to carve the slice. The "
            "operator pool bake passes the box row's recorded user; the default is the gen-2 fleet's."
        ),
    )
    pool_private_key_path: str | None = Field(
        default=None,
        description=(
            "Path (on the machine running the bake) to the private key that opens the box's service user and, "
            "on gen-2, the slice VM's root: the operator's certificate-bearing management identity (its "
            "``-cert.pub`` sibling is presented) on gen-2, the pool management private key on gen-1. Set by "
            "the operator pool bake (``pool create``)."
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
    # CLEANUP: drop this knob (and the gen-1 branches that read it) once the
    # gen-1 -> gen-2 cutover has run on every tier (phase 6 of
    # blueprint/slice-fleet-cutover); gen-2 slices carry no static management key.
    pool_authorized_public_key: str | None = Field(
        default=None,
        description=(
            "Gen-1 only: the pool management public key to authorize for the slice's VM root and inner "
            "container, so the connector can inject the leasing user's key at lease time and reach the VM at "
            "release time. Set by the operator pool bake (``pool create``) for gen-1 boxes."
        ),
    )
    trusted_user_ca_public_key: str | None = Field(
        default=None,
        description=(
            "Gen-2: the tier's SSH CA public key the slice's VM root and inner container trust for "
            "certificate logins (the connector, analytics, and operators present short-lived certificates "
            "instead of a static key). Set by the operator pool bake (``pool create``) for gen-2 boxes; a "
            "gen-2 carve refuses without it."
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
    # are computed by the operator pool bake and passed in per bake via
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
    box_generation: int = Field(
        default=1,
        description=(
            "The target box's slice-fleet generation (specs/slice-fleet-gen2): 1 selects the lima backend, "
            "2 the raw-qemu backend with routed-tap networking. Set by the operator pool bake from the box's "
            "bare_metal_servers row."
        ),
    )
    slice_units: int | None = Field(
        default=None,
        description=(
            "The machine's size in units (1 unit = 1GiB guest RAM; specs/slice-fleet). Required to carve a "
            "gen-2 machine; set by the operator pool bake (dev bakes may override the default via --units)."
        ),
    )
    slice_box_total_units: int | None = Field(
        default=None,
        description=(
            "The target box's sellable unit budget (specs/slice-fleet), the fair-share denominator and the "
            "reserve's memory-budget input. Required for gen-2 carves; computed per box by the pool bake."
        ),
    )
    slice_box_disk_budget_gib: int | None = Field(
        default=None,
        description=(
            "The target box's disk budget in GiB (usable disk minus the reserve; specs/slice-fleet), the "
            "reserve's disk-budget input. Required for gen-2 carves; computed per box by the pool bake."
        ),
    )
    slice_uplink_mbps: int | None = Field(
        default=None,
        description=(
            "The box's declared uplink rate in Mbit/s, sizing gen-2 per-slice fair-share bandwidth classes. "
            "None (or gen 1) disables traffic shaping. Set by the operator pool bake from the box's row."
        ),
    )
    slice_host_id: str | None = Field(
        default=None,
        description=(
            "The host id to give the one slice this create carves (``host-<32 hex>``), so the caller can "
            "record the slice (its derived instance/disk names) in the pool DB *before* the carve. None "
            "generates a fresh id."
        ),
    )
    default_workspace_template_cache_tag: str | None = Field(
        default=None,
        description=(
            "When set (production --from-tag bakes only, e.g. 'default-workspace-template:minds-v0.3.2'), enable the per-box DEFAULT_WORKSPACE_TEMPLATE "
            "image cache: the first slice on the box builds + seeds a box-local 'docker save' tar under this "
            "tag, and subsequent slices 'docker load' it instead of rebuilding. None disables caching "
            "(dev --workspace-dir bakes always build)."
        ),
    )


@pure
def render_remove_authorized_key_command(public_key: str, authorized_keys_path: str) -> str:
    """Shell that drops one key line from an authorized_keys file, emptying it when that was its only line.

    ``grep -v`` exits 1 when nothing is left to print, which is the gen-2 norm
    (VM root authorizes nothing but the bake's transfer key), so that status is
    accepted; any other grep failure still aborts before the file is replaced.
    """
    quoted_path = shlex.quote(authorized_keys_path)
    return (
        f"if [ -f {quoted_path} ]; then "
        f"{{ grep -vF {shlex.quote(public_key)} {quoted_path} || [ $? -eq 1 ]; }} > {quoted_path}.tmp "
        f"&& mv {quoted_path}.tmp {quoted_path}; fi"
    )


class SliceVpsDockerProvider(VpsProvider):
    """A VpsProvider whose 'VPS' is a slice VM we run on a bare-metal box.

    The bake runs from wherever ``mngr create`` is invoked (the operator's laptop,
    like an OVH bake): ``create_host`` carves the VM by driving the box's
    generation-specific slice client over SSH on the box (lima for gen 1, raw qemu
    for gen 2), then reaches the VM's box-forwarded ports to build the container.
    Reuses the shared container bake unchanged; the only differences from a real
    VPS are confined to overridable seams: the outer/inner SSH reach a forwarded
    port on the box (not :22 / :container_ssh_port on a unique IP), and the btrfs
    fs is the slice's data disk mounted at ``btrfs_mount_path`` (so we create the
    per-host subvolume directly, with no loopback image).

    One slice per ``create_host`` call; slice discovery / lease / teardown go
    through the connector + the DB, not this provider, so per-host ports live in
    instance state for the duration of the bake.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The base ``config`` / ``vps_client`` fields hold these same objects (passed
    # at construction); these narrowly-typed aliases expose the slice-specific
    # knobs and the slice client without re-declaring the base fields (which would
    # be an invariant-override type error -- the pattern OvhProvider uses too).
    slice_config: SliceVpsDockerProviderConfig = Field(frozen=True, description="Slice provider configuration")
    slice_client: SliceVmClientInterface = Field(
        frozen=True, description="Generation-specific slice VM client (lima for gen 1, raw qemu for gen 2)"
    )

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

    def _compute_extra_start_args(self) -> tuple[str, ...]:
        # Hard-cap the workspace container's memory so it can never starve the
        # slice VM's own daemons (sshd, dockerd, lima-guestagent) -- an uncapped
        # workspace at capacity collapses the VM-wide page cache and wedges the
        # VM unrecoverably. Known on both container-creation paths: the bake sets
        # slice_memory_mib per box, and the slow-path rebuild derives it from the
        # lease's memory_gb attribute (None only against a legacy row without it,
        # which keeps the previous uncapped behavior).
        memory_mib = self.slice_config.slice_memory_mib
        if memory_mib is None:
            return ()
        return build_slice_container_memory_start_args(memory_mib)

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
        and uploading an SSH key, carves a slice VM on the box (no slice client
        supports cloud ordering) and reaches it via box-forwarded ports.
        """
        host_id = (
            HostId(self.slice_config.slice_host_id)
            if self.slice_config.slice_host_id is not None
            else HostId.generate()
        )
        box = self.slice_config.box_public_address
        env_name = self.slice_config.slice_env_name
        logger.info("Creating slice host {} ({}) on box {} (env={})", name, host_id, box, env_name)

        # This host's unique VPS host keypair is pre-injected as the VM's sshd host
        # key (no first-connect TOFU). On gen-1 the provider's VPS keypair
        # authorizes root on the VM; a gen-2 VM authorizes no static root key and
        # trusts the tier's SSH CA instead (the bake reaches it with a certificate).
        is_gen2 = self.slice_config.box_generation >= FIRST_QEMU_BOX_GENERATION
        vps_public_key: str | None
        if is_gen2:
            vps_public_key = None
        else:
            _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair(host_id)

        instance_id = VpsInstanceId(slice_instance_name(host_id, env_name))
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
                "be set to carve a slice (they are computed per box by the operator pool bake, `pool create`)"
            )
        # Gen-2 carves additionally need the machine-sizing knobs (specs/slice-fleet):
        # the machine's units and the box's two budgets, which the reserve's
        # two-budget accounting enforces on the box.
        if self.slice_config.box_generation >= FIRST_QEMU_BOX_GENERATION and (
            self.slice_config.slice_units is None
            or self.slice_config.slice_box_total_units is None
            or self.slice_config.slice_box_disk_budget_gib is None
        ):
            raise MngrError(
                "slice_units / slice_box_total_units / slice_box_disk_budget_gib must all be set to carve a "
                "gen-2 machine (they are computed per box by the operator pool bake)"
            )
        region = self._resolved_region()
        # Gen-2: VM root and the inner container trust the tier's SSH CA, and no
        # management key is authorized anywhere. Gen-1: the pool management key is
        # authorized on both so the connector can inject the leasing user's key at
        # lease time and reach the VM at release time.
        ssh_authority = resolve_slice_ssh_authority(
            is_gen2=is_gen2,
            trusted_user_ca_public_key=self.slice_config.trusted_user_ca_public_key,
            pool_authorized_public_key=self.slice_config.pool_authorized_public_key,
        )
        extra_root_keys = ssh_authority.extra_root_authorized_keys
        effective_authorized_keys = [*extra_root_keys, *(authorized_keys or ())]
        # Destroy the VM on ANY failure after provisioning (a try/finally + success
        # flag, so we clean up unconditionally without a broad ``except``).
        is_baked = False
        try:
            # Reserve the box slot + host ports (under the box lock) and boot the
            # env-stamped VM. The ports are chosen on the box, so they come back here.
            provision_result = self.slice_client.provision_slice_vm(
                host_id=host_id,
                env_name=env_name,
                vcpus=vcpus,
                memory_mib=memory_mib,
                disk_gib=disk_gib,
                host_dir=str(self.config.btrfs_mount_path),
                root_authorized_public_key=vps_public_key,
                host_private_key_pem=vps_host_key_path.read_text(),
                host_public_key_openssh=vps_host_public_key,
                boot_disk_gib=(
                    GEN2_BOOT_DISK_GIB
                    if self.slice_config.box_generation >= FIRST_QEMU_BOX_GENERATION
                    else SLICE_BOOT_DISK_GIB
                ),
                slot_count=slot_count,
                port_range_start=port_range_start,
                port_range_end=port_range_end,
                extra_root_authorized_keys=extra_root_keys,
                trusted_user_ca_public_key=ssh_authority.vm_trusted_user_ca_public_key,
                uplink_mbps=self.slice_config.slice_uplink_mbps,
                units=self.slice_config.slice_units,
                box_total_units=self.slice_config.slice_box_total_units,
                box_disk_budget_gib=self.slice_config.slice_box_disk_budget_gib,
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
                host_id=host_id,
            )
            wait_for_sshd(hostname=box, port=vm_ssh_port, timeout_seconds=self.config.ssh_connect_timeout)

            with self._make_outer_for_vps_ip(box) as outer:
                # A gen-2 guest's sshd answers while its one-time first boot is
                # still running; wait it out before any outer provisioning
                # (which would otherwise race cloud-init's apt for the dpkg
                # lock and can observe the data disk before its format/mount).
                # Gen-1 needs no wait: ``limactl start`` already blocks until
                # the guest's provision scripts complete.
                if self.slice_config.box_generation >= FIRST_QEMU_BOX_GENERATION:
                    wait_for_guest_cloud_init_to_finish(outer)
                # Production (--from-tag) bakes use the per-box DEFAULT_WORKSPACE_TEMPLATE image cache: ensure the
                # tagged image is present in the slice's dockerd (build + seed it as the first
                # slice on the box, or docker-load the box tar), then run it as-is instead of
                # rebuilding. Dev bakes leave default_workspace_template_cache_tag None and build from the Dockerfile.
                default_workspace_template_cache_tag = self.slice_config.default_workspace_template_cache_tag
                if default_workspace_template_cache_tag is not None:
                    self._ensure_cached_image_present(
                        outer=outer,
                        host_id=host_id,
                        vm_ssh_port=vm_ssh_port,
                        image_tag=default_workspace_template_cache_tag,
                        build_args=build_args,
                    )
                    create_image: ImageReference | None = ImageReference(default_workspace_template_cache_tag)
                    create_build_args: Sequence[str] | None = ()
                    is_local_image_used = True
                else:
                    create_image = image
                    create_build_args = build_args
                    is_local_image_used = False
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
                    image=create_image,
                    tags=tags,
                    build_args=create_build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=effective_authorized_keys,
                    allow_local_image=is_local_image_used,
                    extra_ssh_config_files=ssh_authority.container_ssh_config_files,
                )
            logger.info("Slice host {} created (instance {})", name, instance_id)
            is_baked = True
            return host
        finally:
            if not is_baked:
                logger.error("Slice host creation failed, destroying VM {}", instance_id)
                try:
                    self.slice_client.destroy_instance(instance_id)
                except MngrError as cleanup_err:
                    logger.warning("Failed to clean up slice VM {}: {}", instance_id, cleanup_err)

    # ------------------------------------------------------------------
    # Per-box DEFAULT_WORKSPACE_TEMPLATE image cache (build once per box, docker-load per slice)
    # ------------------------------------------------------------------

    def _make_box_image_cache(self) -> BoxImageCacheInterface:
        return SshBoxImageCache(
            slice_client=self.slice_client,
            cache_dir=box_image_cache_dir_for_generation(
                self.slice_config.box_generation, self.slice_config.box_ssh_user
            ),
        )

    def _ensure_cached_image_present(
        self,
        *,
        outer: OuterHostInterface,
        host_id: HostId,
        vm_ssh_port: int,
        image_tag: str,
        build_args: Sequence[str] | None,
    ) -> None:
        """Make image_tag present in the slice's dockerd: load the box tar, or seed it as the first slice.

        Block-then-load with dead-seeder handoff: only the build-lock holder builds
        + seeds; everyone else waits for the tar then loads. The wait returns early
        when the lock disappears without a tar (the seeder died or its build
        failed), and every round re-checks the tar and re-contends the lock -- so a
        failed seed hands off to a new builder within seconds instead of stranding
        the waiters for the full wait window. Rounds are bounded so repeated seed
        failures (or a wedged builder outliving the stale-lock TTL reclaim) surface
        as an error rather than waiting forever.
        """
        cache = self._make_box_image_cache()
        for _round_idx in range(_IMAGE_CACHE_WAIT_ROUNDS):
            # Re-checked every round: a seed that published while we contended the
            # lock (or between the lock vanishing and our wait returning) is a hit.
            if cache.has_tar(image_tag):
                self._load_cached_image(cache=cache, outer=outer, vm_ssh_port=vm_ssh_port, image_tag=image_tag)
                return
            if cache.try_acquire_build_lock(image_tag):
                try:
                    self._seed_box_image(
                        cache=cache,
                        outer=outer,
                        host_id=host_id,
                        vm_ssh_port=vm_ssh_port,
                        image_tag=image_tag,
                        build_args=build_args,
                    )
                finally:
                    cache.release_build_lock(image_tag)
                return
            # Another slice is seeding: wait for it to publish the tar or die.
            # Either way the next round re-checks the tar and re-contends the lock.
            cache.wait_for_tar(image_tag, timeout_seconds=WAIT_FOR_TAR_TIMEOUT_SECONDS)
        # One final check: a tar published during an earlier round's wait is caught
        # by the next round's re-check, but the LAST round's wait has no following
        # round -- load rather than fail on a tar that is right there.
        if cache.has_tar(image_tag):
            self._load_cached_image(cache=cache, outer=outer, vm_ssh_port=vm_ssh_port, image_tag=image_tag)
            return
        raise BoxImageCacheError(
            f"gave up waiting for the box DEFAULT_WORKSPACE_TEMPLATE image tar for {image_tag} "
            f"after {_IMAGE_CACHE_WAIT_ROUNDS} rounds"
        )

    def _load_cached_image(
        self, *, cache: BoxImageCacheInterface, outer: OuterHostInterface, vm_ssh_port: int, image_tag: str
    ) -> None:
        logger.info("Loading DEFAULT_WORKSPACE_TEMPLATE image {} from box tar into slice", image_tag)
        with self._transfer_key_authorized(cache, outer) as transfer_key:
            cache.load_image_into_slice(image_tag, vm_ssh_port=vm_ssh_port, transfer_key=transfer_key)
        logger.info("Loaded DEFAULT_WORKSPACE_TEMPLATE image {} from box tar", image_tag)

    def _seed_box_image(
        self,
        *,
        cache: BoxImageCacheInterface,
        outer: OuterHostInterface,
        host_id: HostId,
        vm_ssh_port: int,
        image_tag: str,
        build_args: Sequence[str] | None,
    ) -> None:
        """Build the DEFAULT_WORKSPACE_TEMPLATE image (+ baked Playwright) and seed the box tar; this slice runs that image too."""
        logger.info("Building + seeding box tar {} (first slice on this box for this tag)", image_tag)
        parsed = self._parse_build_args(build_args)
        # Build the base DEFAULT_WORKSPACE_TEMPLATE image via the same shared helper the realizer's build path
        # uses (DockerRealizer._build_image_on_vps) -- so any future build preprocessing
        # belongs in build_image_on_outer_from_build_args, not the per-caller wrapper, and
        # is picked up here too. We pass a longer timeout than the realizer default because
        # the DEFAULT_WORKSPACE_TEMPLATE build is the seed's long pole.
        base_image = build_image_on_outer_from_build_args(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            docker_build_args=parsed.docker_build_args,
            git_depth=parsed.git_depth,
            builder=self.config.builder,
            build_timeout_seconds=_SEED_BASE_BUILD_TIMEOUT_SECONDS,
        )
        self._build_playwright_derived_image(outer=outer, base_image=base_image, target_tag=image_tag)
        cache.check_free_disk(_ESTIMATED_DEFAULT_WORKSPACE_TEMPLATE_IMAGE_BYTES)
        with self._transfer_key_authorized(cache, outer) as transfer_key:
            cache.save_image_from_slice(image_tag, vm_ssh_port=vm_ssh_port, transfer_key=transfer_key)
        # Reclaim the builder slice's build-cache headroom so it matches a loading slice.
        run_docker(outer, ["builder", "prune", "-af"], timeout_seconds=_BUILDER_PRUNE_TIMEOUT_SECONDS)
        logger.info("Built + seeded box tar {}", image_tag)

    @retry(
        retry=retry_if_exception_type(MngrError),
        stop=stop_after_attempt(_PLAYWRIGHT_BUILD_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _build_playwright_derived_image(self, *, outer: OuterHostInterface, base_image: str, target_tag: str) -> None:
        """Build target_tag as base_image + a baked Fortress/Chromium layer.

        The browser engine is deliberately not in the DEFAULT_WORKSPACE_TEMPLATE Dockerfile (it is
        shared with the desktop Lima path); baking it cloud-side here -- by running the exact
        env.d unit the workspace runs at boot -- keeps the desktop path unchanged while letting
        every loaded slice's env-converge unit hit its fast satisfied-check (the engine binary is
        already in place; no marker files are involved).

        The RUN first guards that the DEFAULT_WORKSPACE_TEMPLATE build-code dir exists, so a future DEFAULT_WORKSPACE_TEMPLATE image
        that relocates it fails fast with a clear message instead of a confusing
        ``cd``-not-found build failure buried in retries.

        The unit invokes playwright as ``python -m playwright`` (not the ``playwright``
        console script) on purpose: the DEFAULT_WORKSPACE_TEMPLATE Dockerfile builds the uv venv at
        ``/home/user/workspace`` and then ``mv``\\s the workspace to ``/docker_build_code``. A uv
        venv is path-bound -- its console-script shebangs hardcode the original path, which does
        not exist here, so the console script would fail with ``Failed to spawn: playwright``.
        ``python -m`` goes through the venv's interpreter symlink (location-independent), so it
        works from the relocated path.
        """
        guard = (
            f"test -d {_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR} || "
            f"{{ echo 'DEFAULT_WORKSPACE_TEMPLATE build-code dir {_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR} missing; DEFAULT_WORKSPACE_TEMPLATE image layout changed -- "
            "update _DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR' >&2; exit 1; }"
        )
        dockerfile = (
            f"FROM {base_image}\n"
            f"RUN {guard} "
            f"&& cd {_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR} "
            f"&& ENV_CONVERGE_WORKSPACE_DIR={_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR} bash {_ENV_D_BROWSER_UNIT}\n"
        )
        encoded_dockerfile = base64.b64encode(dockerfile.encode()).decode()
        stage_command = (
            f"rm -rf {_PLAYWRIGHT_CTX_DIR} && mkdir -p {_PLAYWRIGHT_CTX_DIR} && "
            f"echo {shlex.quote(encoded_dockerfile)} | base64 -d > {_PLAYWRIGHT_CTX_DIR}/Dockerfile"
        )
        stage_result = outer.execute_idempotent_command(stage_command, timeout_seconds=30.0)
        if not stage_result.success:
            raise BoxImageCacheError(
                f"failed to stage the Playwright Dockerfile on the slice: {stage_result.stderr.strip()}"
            )
        run_docker(
            outer,
            ["build", "-t", target_tag, "-f", f"{_PLAYWRIGHT_CTX_DIR}/Dockerfile", _PLAYWRIGHT_CTX_DIR],
            timeout_seconds=_PLAYWRIGHT_BUILD_TIMEOUT_SECONDS,
        )

    @contextmanager
    def _transfer_key_authorized(
        self, cache: BoxImageCacheInterface, outer: OuterHostInterface
    ) -> Iterator[TransferKey]:
        """Yield a unique ephemeral transfer key authorized on the slice's VM root; tear it down after.

        The box uses the key to docker save/load over its own loopback to the slice's
        VM-root sshd. The key is destroyed (private key off the box, public key out of
        the slice's authorized_keys) whether the transfer succeeds or fails, so no
        standing box->slice root key survives the bake.
        """
        transfer_key = cache.create_transfer_key()
        try:
            self._authorize_transfer_key(outer, transfer_key.public_key)
            yield transfer_key
        finally:
            cache.destroy_transfer_key(transfer_key)
            self._deauthorize_transfer_key(outer, transfer_key.public_key)

    def _authorize_transfer_key(self, outer: OuterHostInterface, public_key: str) -> None:
        command = (
            f"install -d -m 700 {_VM_ROOT_SSH_DIR} && "
            f"printf '%s\\n' {shlex.quote(public_key)} >> {_VM_ROOT_AUTHORIZED_KEYS_PATH}"
        )
        result = outer.execute_idempotent_command(command, timeout_seconds=30.0)
        if not result.success:
            raise BoxImageCacheError(f"failed to authorize the transfer key on the slice: {result.stderr.strip()}")

    def _deauthorize_transfer_key(self, outer: OuterHostInterface, public_key: str) -> None:
        # Best-effort: teardown runs in a finally and must not mask a prior error.
        command = render_remove_authorized_key_command(public_key, _VM_ROOT_AUTHORIZED_KEYS_PATH)
        result = outer.execute_idempotent_command(command, timeout_seconds=30.0)
        if not result.success:
            logger.warning(
                "Failed to remove the transfer key from the slice authorized_keys: {}", result.stderr.strip()
            )

    # ------------------------------------------------------------------
    # Per-host-port seam overrides (the bake reaches the VM via box:port)
    # ------------------------------------------------------------------

    def _vm_root_private_key_path(self) -> Path:
        """The key that opens a slice's VM root: the certificate-bearing management identity on gen-2, the VPS keypair on gen-1.

        The gen-2 identity is the same key + ``-cert.pub`` the bake dials the box
        with (the operator's Vault-signed certificate carries the VM principal), so
        no static key of ours is ever authorized on a gen-2 VM.
        """
        if self.slice_config.box_generation >= FIRST_QEMU_BOX_GENERATION:
            if self.slice_config.pool_private_key_path is None:
                raise MngrError(
                    "pool_private_key_path must be set to reach a gen-2 slice VM (the management identity)"
                )
            return Path(self.slice_config.pool_private_key_path)
        vps_key_path, _pub = self._get_vps_ssh_keypair()
        return vps_key_path

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        port = self._current_outer_port if self._current_outer_port is not None else 22
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=port,
            private_key_path=self._vm_root_private_key_path(),
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

    def _wait_for_container_sshd(self, vps_ip: str, host_id: HostId, realizer: HostRealizer | None = None) -> None:
        # imbue_cloud is container-only (it rejects bare), and the agent sshd is
        # reached on a dynamically forwarded port, so the realizer (and the
        # host_id it would resolve a key for) is unused here.
        del host_id, realizer
        port = (
            self._current_container_port
            if self._current_container_port is not None
            else self.config.container_ssh_port
        )
        wait_for_sshd(hostname=vps_ip, port=port, timeout_seconds=self.config.ssh_connect_timeout)

    def _create_host_object(self, host_id: HostId, host_name: HostName, vps_ip: str, realizer: HostRealizer) -> Host:
        container_key_path, _container_pub = self._get_container_ssh_keypair(host_id)
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
            host_id=host_id,
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
