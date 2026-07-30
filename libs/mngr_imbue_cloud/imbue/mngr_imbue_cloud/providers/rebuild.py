from collections.abc import Mapping
from typing import Any

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.data_types import LeaseResult
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
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


@pure
def _build_delegated_vps_config(config: ImbueCloudProviderConfig) -> VpsProviderConfig:
    """Build the delegated vps_docker config for the slow-path rebuild.

    Forwards the runtime knobs (``docker_runtime`` / ``install_gvisor_runtime`` /
    ``default_start_args``) from the imbue_cloud config so the rebuilt container
    runs under the configured runtime with the configured hardening args.
    """
    return VpsProviderConfig(
        backend=ProviderBackendName("vps_docker"),
        host_dir=config.host_dir,
        container_ssh_port=config.container_ssh_port,
        docker_runtime=config.docker_runtime,
        install_gvisor_runtime=config.install_gvisor_runtime,
        default_start_args=config.default_start_args,
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

    Forwards the runtime knobs from ``config`` (an ``ImbueCloudProviderConfig``,
    which extends ``VpsProviderConfig``) so the rebuilt container runs under
    the configured runtime with the configured hardening args -- e.g.
    ``docker_runtime='runsc'`` plus ``--workdir=/`` /
    ``--security-opt=no-new-privileges`` from ``default_start_args``, which minds
    bootstrap writes into the per-account block.
    """
    vps_config = _build_delegated_vps_config(config)
    return MinimalVpsProvider(
        name=name,
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=vps_config,
        vps_client=ExternallyManagedVpsClient(),
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
    slice_config = SliceVpsDockerProviderConfig(
        host_dir=config.host_dir,
        container_ssh_port=config.container_ssh_port,
        box_public_address=lease_result.vps_address,
        slice_memory_mib=slice_memory_mib,
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
