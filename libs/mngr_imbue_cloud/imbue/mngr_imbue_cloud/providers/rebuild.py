from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_TMPFS_START_ARGS
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.slice_client import build_slice_vm_client
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.instance import MinimalVpsProvider
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient


@pure
def _slice_memory_mib_from_lease(lease_result: LeaseResult) -> int | None:
    """The RAM the leased machine's guest actually has, in MiB (None when its size is unknown).

    The container cap is derived from this exactly as the bake derives it: a gen-2
    guest boots with its units minus the per-machine holdback (what its own
    reconcile oneshot sees in MemTotal), a gen-1 lima guest with its full units.
    """
    # CLEANUP: make ``LeaseResult.memory_units`` required and drop this None
    # branch once every tier's connector serves the sizing columns (the release
    # carrying the slice-fleet cutover deployed to staging and production).
    units = lease_result.memory_units
    if units is None or units <= 0:
        return None
    if lease_result.box_generation >= FIRST_QEMU_BOX_GENERATION:
        return compute_machine_guest_memory_mib(units)
    return compute_slice_memory_mib(units)


# Every field the delegated rebuild providers share with the account config:
# the rebuild must carve and run the container exactly as a provider created
# under this config would, so the whole VpsProviderConfig surface is forwarded
# structurally rather than field by field (a hand-copied list silently drops
# any knob it does not name).
_DELEGATED_FIELDS: Final[frozenset[str]] = frozenset(VpsProviderConfig.model_fields) - {"backend"}
# On a slice the container runtime and start args follow the slice's
# generation (see build_slice_rebuild_config): a gen-1 lima guest has no runsc,
# so the account block's gVisor knobs cannot be forwarded as-is, and the runsc
# host setup is never run on a slice VM.
_SLICE_DELEGATED_FIELDS: Final[frozenset[str]] = _DELEGATED_FIELDS - {
    "docker_runtime",
    "install_gvisor_runtime",
    "default_start_args",
}


@pure
def _delegated_vps_fields(config: ImbueCloudProviderConfig, fields: frozenset[str]) -> dict[str, Any]:
    """The named VpsProviderConfig fields of the account config, ready to re-validate into a delegated config."""
    return config.model_dump(include=set(fields))


@pure
def _build_delegated_vps_config(config: ImbueCloudProviderConfig) -> VpsProviderConfig:
    """Build the delegated vps_docker config for the slow-path rebuild.

    Forwards every VpsProviderConfig field of the imbue_cloud config -- the
    runtime knobs (``docker_runtime`` / ``install_gvisor_runtime`` /
    ``default_start_args``), the user-data layout knobs (``volume_home_path`` /
    ``host_log_dir``), and the rest -- so the rebuilt container runs under the
    configured runtime with the configured hardening args and gets the same
    volume layout as a baked one.
    """
    return VpsProviderConfig(
        backend=ProviderBackendName("vps_docker"), **_delegated_vps_fields(config, _DELEGATED_FIELDS)
    )


def build_delegated_vps_provider(
    *,
    name: ProviderInstanceName,
    config: ImbueCloudProviderConfig,
    mngr_ctx: MngrContext,
) -> VpsProvider:
    """Construct a vps_docker provider bound to an imbue_cloud instance's keys/config.

    It only ever runs ``teardown_container_on_existing_vps`` /
    ``create_host_on_existing_vps`` (which take a caller-supplied ``outer``
    and make no VPS-API calls), so its ``vps_client`` is the
    ``ExternallyManagedVpsClient`` stub that raises on any ordering call.

    Forwards every VpsProviderConfig field of ``config`` (an
    ``ImbueCloudProviderConfig``, which extends ``VpsProviderConfig``; see
    ``_build_delegated_vps_config``) so the rebuilt container runs under the
    configured runtime with the configured hardening args and volume layout --
    e.g. ``docker_runtime='runsc'`` plus ``--workdir=/`` /
    ``--security-opt=no-new-privileges`` from ``default_start_args``, and
    ``volume_home_path='/home/user'``, as minds writes into the per-account
    block.
    """
    vps_config = _build_delegated_vps_config(config)
    return MinimalVpsProvider(
        name=name,
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=vps_config,
        vps_client=ExternallyManagedVpsClient(),
    )


@pure
def build_slice_rebuild_config(
    config: ImbueCloudProviderConfig, lease_result: LeaseResult
) -> SliceVpsDockerProviderConfig:
    """The slice provider config for rebuilding the container on a leased slice.

    Forwards every VpsProviderConfig field of the imbue_cloud config except the
    generation-dependent runtime knobs (see ``_SLICE_DELEGATED_FIELDS``) and
    layers the slice's coordinates on top. A gen-2 slice's guest ships the
    gVisor runtime in its image, so the rebuilt container runs under the account
    config's ``docker_runtime`` (the per-account block sets ``runsc``) with its
    hardening ``default_start_args`` plus the gen-2 tmpfs mounts -- exactly the
    shape the bake creates. A gen-1 (lima) guest has no runsc, so its rebuild
    stays plain runc with no extra args.
    """
    # The guest's RAM (from the lease's sizing column) sizes the rebuilt
    # container's memory cap, exactly as the bake sizes the original container's.
    slice_memory_mib = _slice_memory_mib_from_lease(lease_result)
    is_gen2 = lease_result.box_generation >= FIRST_QEMU_BOX_GENERATION
    return SliceVpsDockerProviderConfig(
        **_delegated_vps_fields(config, _SLICE_DELEGATED_FIELDS),
        box_public_address=lease_result.vps_address,
        box_generation=lease_result.box_generation,
        slice_memory_mib=slice_memory_mib,
        docker_runtime=config.docker_runtime if is_gen2 else None,
        default_start_args=(tuple(config.default_start_args) + GEN2_CONTAINER_TMPFS_START_ARGS if is_gen2 else ()),
    )


def build_slice_rebuild_provider(
    *,
    name: ProviderInstanceName,
    config: ImbueCloudProviderConfig,
    mngr_ctx: MngrContext,
    lease_result: LeaseResult,
) -> SliceVpsDockerProvider:
    """Construct a slice provider to rebuild the container on a leased slice VM.

    A slice's container is published inside the VM on the standard guest port
    (``container_ssh_port``, which the box forwards to a host port) but is
    reached from outside at the lease's forwarded ``container_ssh_port`` / VM
    root ``ssh_port``. The slice provider already splits publish vs connect
    ports via these per-host-port fields, so the rebuild (teardown +
    ``create_host_on_existing_vps``) targets the right ports. The container
    runtime and start args follow the lease's generation
    (:func:`build_slice_rebuild_config`).
    """
    slice_config = build_slice_rebuild_config(config, lease_result)
    if slice_config.slice_memory_mib is None:
        logger.warning(
            "Lease {} carries no machine size (an older connector); rebuilding the container without a memory cap",
            lease_result.host_db_id,
        )
    # The rebuild never carves/destroys a VM (it only tears down + rebuilds the
    # container on the already-leased slice via the forwarded ports below), so
    # the slice client's box-SSH coordinates are unused here regardless of the
    # box's generation; pass the address for completeness and no pool key (no
    # box-side slice command is ever invoked on this path).
    slice_client = build_slice_vm_client(
        box_generation=lease_result.box_generation,
        box_address=lease_result.vps_address,
        box_ssh_port=22,
        box_ssh_user=slice_config.box_ssh_user,
        private_key_path=None,
        box_host_public_key=None,
    )
    provider = SliceVpsDockerProvider(
        name=name,
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=slice_config,
        vps_client=slice_client,
        slice_config=slice_config,
        slice_client=slice_client,
    )
    # Point the per-host-port seams at the lease's box-forwarded ports so the
    # rebuild's outer (VM root) and container connections target the box.
    provider.set_forwarded_ports(
        outer_port=lease_result.ssh_port,
        container_port=lease_result.container_ssh_port,
    )
    return provider
