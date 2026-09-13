from collections.abc import Mapping
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
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.instance import MinimalVpsProvider
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient


@pure
def _slice_memory_mib_from_lease_attributes(attributes: Mapping[str, Any]) -> int | None:
    """The leased slice's RAM in MiB, from its row's stamped ``memory_gb`` attribute.

    Every slice row is stamped with ``memory_gb`` at bake time; None (legacy rows
    or a non-numeric value) means the rebuilt container gets no memory cap.
    """
    memory_gb = attributes.get("memory_gb")
    if isinstance(memory_gb, bool) or not isinstance(memory_gb, (int, float)):
        return None
    if memory_gb <= 0:
        return None
    return int(memory_gb * 1024)


# Every field the delegated rebuild providers share with the account config:
# the rebuild must carve and run the container exactly as a provider created
# under this config would, so the whole VpsProviderConfig surface is forwarded
# structurally rather than field by field (a hand-copied list silently drops
# any knob it does not name).
_DELEGATED_FIELDS: Final[frozenset[str]] = frozenset(VpsProviderConfig.model_fields) - {"backend"}
# A slice VM is the isolation boundary and its Docker is plain runc (the lima
# provision script installs no runsc, and the rebuild skips the runsc host
# setup for slices), so the account block's gVisor knobs must stay off the
# slice config: with them the rebuilt container's `docker run --runtime runsc`
# fails on the VM.
_SLICE_DELEGATED_FIELDS: Final[frozenset[str]] = _DELEGATED_FIELDS - {"docker_runtime", "install_gvisor_runtime"}


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
def _build_slice_rebuild_config(
    config: ImbueCloudProviderConfig,
    *,
    box_public_address: str,
    slice_memory_mib: int | None,
) -> SliceVpsDockerProviderConfig:
    """Build the slice provider config for the slow-path rebuild on a leased slice.

    Forwards every VpsProviderConfig field of the imbue_cloud config except the
    gVisor knobs (see ``_SLICE_DELEGATED_FIELDS``) and layers the slice-specific
    coordinates on top; the slice class keeps its own backend name and
    slice-only defaults.
    """
    return SliceVpsDockerProviderConfig(
        **_delegated_vps_fields(config, _SLICE_DELEGATED_FIELDS),
        box_public_address=box_public_address,
        slice_memory_mib=slice_memory_mib,
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
    (``container_ssh_port``, which lima forwards to a box host port) but is
    reached from outside at the lease's forwarded ``container_ssh_port`` / VM
    root ``ssh_port``. The slice provider already splits publish vs connect
    ports via these per-host-port fields, so the rebuild (teardown +
    ``create_host_on_existing_vps``) targets the right ports. runsc/gVisor is
    not used (the VM is the isolation boundary; its Docker is plain runc).
    """
    # The slice's RAM (stamped on its row) sizes the rebuilt container's memory
    # cap, exactly as the bake sizes the original container's.
    slice_memory_mib = _slice_memory_mib_from_lease_attributes(lease_result.attributes)
    if slice_memory_mib is None:
        logger.warning(
            "Lease {} has no usable memory_gb attribute; rebuilding the container without a memory cap",
            lease_result.host_db_id,
        )
    slice_config = _build_slice_rebuild_config(
        config, box_public_address=lease_result.vps_address, slice_memory_mib=slice_memory_mib
    )
    # The rebuild never carves/destroys a VM (it only tears down + rebuilds the
    # container on the already-leased slice via the forwarded ports below), so
    # the lima client's box-SSH coordinates are unused here; pass the address
    # for completeness and no pool key (limactl is never invoked on this path).
    lima_client = LimaSliceVpsClient(
        box_address=lease_result.vps_address,
        box_ssh_user=slice_config.box_ssh_user,
        private_key_path=None,
    )
    provider = SliceVpsDockerProvider(
        name=name,
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=slice_config,
        vps_client=lima_client,
        slice_config=slice_config,
        lima_client=lima_client,
    )
    # Point the per-host-port seams at the lease's box-forwarded ports so the
    # rebuild's outer (VM root) and container connections target the box.
    provider.set_forwarded_ports(
        outer_port=lease_result.ssh_port,
        container_port=lease_result.container_ssh_port,
    )
    return provider
