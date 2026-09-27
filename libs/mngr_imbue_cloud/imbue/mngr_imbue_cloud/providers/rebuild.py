from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_TMPFS_START_ARGS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.qemu_slice_client import QemuSliceVpsClient
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.instance import MinimalVpsProvider
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient


@pure
def _slice_memory_mib_from_lease(lease_result: LeaseResult) -> int:
    """The RAM the leased machine's guest actually has, in MiB.

    The container cap is derived from this exactly as the bake derives it: the
    guest boots with its units minus the per-machine holdback (what its own
    reconcile oneshot sees in MemTotal). A lease that carries no size cannot
    be capped like the baked machine, so it is refused rather than left
    uncapped.
    """
    units = lease_result.memory_units
    if units is None or units <= 0:
        raise MngrError(
            f"lease {lease_result.host_db_id} carries no machine size (memory_units={units!r}); the connector "
            "must serve the sizing columns for the rebuilt container to be capped like the baked one"
        )
    return compute_machine_guest_memory_mib(units)


# Every field the delegated rebuild providers share with the account config:
# the rebuild must carve and run the container exactly as a provider created
# under this config would, so the whole VpsProviderConfig surface is forwarded
# structurally rather than field by field (a hand-copied list silently drops
# any knob it does not name).
_DELEGATED_FIELDS: Final[frozenset[str]] = frozenset(VpsProviderConfig.model_fields) - {"backend"}
# The slice guest ships runsc in its image, so the runsc host setup is never
# run on a slice VM and the start args gain the slice tmpfs mounts (see
# build_slice_rebuild_config).
_SLICE_DELEGATED_FIELDS: Final[frozenset[str]] = _DELEGATED_FIELDS - {
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

    Forwards every VpsProviderConfig field of the imbue_cloud config (see
    ``_SLICE_DELEGATED_FIELDS``) and layers the slice's coordinates on top. The
    slice's guest ships the gVisor runtime in its image, so the rebuilt container
    runs under the account config's ``docker_runtime`` (the per-account block
    sets ``runsc``) with its hardening ``default_start_args`` plus the slice
    tmpfs mounts -- exactly the shape the bake creates.
    """
    # The guest's RAM (from the lease's sizing column) sizes the rebuilt
    # container's memory cap, exactly as the bake sizes the original container's.
    return SliceVpsDockerProviderConfig(
        **_delegated_vps_fields(config, _SLICE_DELEGATED_FIELDS),
        box_public_address=lease_result.vps_address,
        slice_memory_mib=_slice_memory_mib_from_lease(lease_result),
        default_start_args=tuple(config.default_start_args) + GEN2_CONTAINER_TMPFS_START_ARGS,
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
    ``create_host_on_existing_vps``) targets the right ports.
    """
    slice_config = build_slice_rebuild_config(config, lease_result)
    # The rebuild never carves/destroys a VM (it only tears down + rebuilds the
    # container on the already-leased slice via the forwarded ports below), so
    # the slice client's box-SSH coordinates are unused here; pass the address
    # for completeness and no management key (no box-side slice command is ever
    # invoked on this path).
    slice_client = QemuSliceVpsClient(
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
