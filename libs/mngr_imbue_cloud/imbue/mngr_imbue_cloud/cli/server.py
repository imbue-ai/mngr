"""``mngr imbue_cloud admin server ...`` -- operator-only bare-metal fleet management.

Manages the OVH bare-metal servers we rent (the lima-VM "slices" we carve on them
are baked via ``admin pool create --backend slice``, whose shared implementation
lives here as :func:`allocate_slices`). Writes the connector's host_pool Neon DB
directly (laptop-side), mirroring ``admin pool create``; the connector only reads
these rows (plus its release-time teardown). Every step is resumable: ordering and
OS install can take a long time, and re-running advances a box one step. The
OVH-touching steps act on the real account and are validated against a delivered
box; ``list`` / ``register`` are exercised without OVH.
"""

import base64
import json
import os
import shlex
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import click
import psycopg2
from loguru import logger
from tabulate import tabulate

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ObservableThread
from imbue.imbue_common.logging import log_span
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.bake.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.mngr_imbue_cloud.bake.pool_bake import BakedPoolHost
from imbue.mngr_imbue_cloud.bake.pool_bake import PoolBakeError
from imbue.mngr_imbue_cloud.bake.pool_bake import bake_pool_host
from imbue.mngr_imbue_cloud.bake.pool_bake import finalize_baked_pool_host
from imbue.mngr_imbue_cloud.bake.pool_bake import sync_mngr_into_template
from imbue.mngr_imbue_cloud.bake.pool_bake import wait_for_deferred_install
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import resolve_pool_database_url
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import SlicePricingRow
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import OVH_US_DATACENTER_CODES
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_MEMORY_PER_SLICE_GB
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.slices.bare_metal import choose_server_for_new_slice
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.slices.bare_metal import partition_port_range
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.bare_metal_db import build_slice_pool_host_insert_values
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_server_by_id
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_server_capacities
from imbue.mngr_imbue_cloud.slices.bare_metal_db import insert_bare_metal_server
from imbue.mngr_imbue_cloud.slices.bare_metal_db import insert_slice_pool_host
from imbue.mngr_imbue_cloud.slices.bare_metal_db import update_server
from imbue.mngr_imbue_cloud.slices.bare_metal_prep import DEFAULT_LIMA_VERSION
from imbue.mngr_imbue_cloud.slices.bare_metal_prep import build_box_prep_script
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.slices.ordering import DEFAULT_REINSTALL_OS_TEMPLATE
from imbue.mngr_imbue_cloud.slices.ordering import build_and_assign_eco_cart
from imbue.mngr_imbue_cloud.slices.ordering import checkout_eco_cart
from imbue.mngr_imbue_cloud.slices.ordering import delete_cart_quietly
from imbue.mngr_imbue_cloud.slices.ordering import derive_server_specs
from imbue.mngr_imbue_cloud.slices.ordering import start_os_reinstall
from imbue.mngr_imbue_cloud.slices.ordering import summarize_checkout_prices
from imbue.mngr_imbue_cloud.slices.ordering import wait_for_dedicated_server_address
from imbue.mngr_imbue_cloud.slices.ordering import wait_for_order_service_name
from imbue.mngr_imbue_cloud.slices.ordering import wait_for_os_reinstall
from imbue.mngr_imbue_cloud.slices.pricing import compute_slice_pricing_rows
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_vps_docker.primitives import VpsInstanceId


def _format_capacity_table(capacities: list[BareMetalServerCapacity]) -> str:
    """Render the server capacity table (one row per box + a fleet total)."""
    header = f"{'ID':<38}{'PLAN':<20}{'REGION':<8}{'STATUS':<12}{'ADDRESS':<18}{'SLOTS(used/total)':>18}"
    lines = [header]
    total_slots = 0
    total_used = 0
    for capacity in capacities:
        server = capacity.server
        total_slots += server.slot_count
        total_used += capacity.used_slots
        lines.append(
            f"{str(server.id):<38}{server.plan_code[:19]:<20}{server.region[:7]:<8}"
            f"{str(server.status):<12}{str(server.public_address or '-')[:17]:<18}"
            f"{f'{capacity.used_slots}/{server.slot_count}':>18}"
        )
    lines.append(
        f"\nFLEET: {len(capacities)} servers, {total_used}/{total_slots} slots used, {total_slots - total_used} free"
    )
    return "\n".join(lines)


@click.group(name="server")
def server() -> None:
    """Bare-metal server fleet management (pricing / order / await-delivery / setup / list / register / set-status)."""


@contextmanager
def _pool_private_key_path() -> Iterator[Path]:
    """Yield a 0600 temp file holding the pool management private key (from POOL_SSH_PRIVATE_KEY).

    The temp directory is removed on exit so the sensitive private key never
    lingers on the operator's disk after the command finishes.
    """
    pem = os.environ.get("POOL_SSH_PRIVATE_KEY")
    if not pem:
        raise BareMetalProvisioningError(
            "POOL_SSH_PRIVATE_KEY is not set; needed to SSH the box. Export it from the env's pool-ssh secret."
        )
    key_dir = Path(tempfile.mkdtemp(prefix="mngr-pool-key-"))
    try:
        key_path = key_dir / "id"
        key_path.write_text(pem if pem.endswith("\n") else pem + "\n")
        key_path.chmod(0o600)
        yield key_path
    finally:
        shutil.rmtree(key_dir, ignore_errors=True)


def _derive_public_key(private_key_path: Path) -> str:
    """Derive the OpenSSH public key from a private key file via ssh-keygen -y."""
    cg = ConcurrencyGroup(name="ssh-keygen")
    with cg:
        result = cg.run_process_to_completion(
            command=["ssh-keygen", "-y", "-f", str(private_key_path)],
            timeout=30.0,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise BareMetalProvisioningError(f"ssh-keygen -y failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_root_script_over_ssh(server_address: str, ssh_user: str, private_key_path: Path, script: str) -> None:
    """Pipe a bash script to ``sudo bash`` on the box over SSH (base64 to dodge quoting)."""
    encoded = base64.b64encode(script.encode()).decode()
    remote = f"echo {encoded} | base64 -d | sudo bash"
    cg = ConcurrencyGroup(name="box-prep-ssh")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                "ssh",
                "-i",
                str(private_key_path),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=30",
                f"{ssh_user}@{server_address}",
                remote,
            ],
            timeout=600.0,
            is_checked_after=False,
            on_output=lambda line, _is_stdout: logger.info("  [box] {}", line.rstrip()),
        )
    if result.returncode != 0:
        raise BareMetalProvisioningError(
            f"box prep on {server_address} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


@server.command(name="prep")
@click.option("--server-address", required=True, help="SSH-reachable address of the freshly-installed box.")
@click.option("--ssh-user", default="debian", help="Bootstrap SSH user (the OS image's default cloud user).")
@click.option("--lima-service-user", default="limahost", help="Dedicated non-root user to create for the lima VMs.")
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box.")
@click.option(
    "--slice-base-image-url",
    default=DEFAULT_IMAGE_URL_X86_64,
    show_default=True,
    help="Guest OS image to stage on the box once (slices boot from this via file://, never the mirror).",
)
def prep_box(
    server_address: str, ssh_user: str, lima_service_user: str, lima_version: str, slice_base_image_url: str
) -> None:
    """Install QEMU + lima + tooling on a delivered box, create the lima user, stage the OS image.

    Idempotent. Authorizes the pool management key (POOL_SSH_PRIVATE_KEY) for the
    service user so the admin CLI can bake slices and the connector can tear them
    down, and stages the slice guest OS image once so bakes never depend on the
    Debian mirror. Run after the OS install, before ``admin pool create --backend slice``.
    """
    with _pool_private_key_path() as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        script = build_box_prep_script(
            pool_public_key=pool_public_key,
            lima_service_user=lima_service_user,
            lima_version=lima_version,
            slice_base_image_url=slice_base_image_url,
        )
        logger.info(
            "Prepping box {} as {} (lima user {}, lima {})", server_address, ssh_user, lima_service_user, lima_version
        )
        _run_root_script_over_ssh(server_address, ssh_user, private_key_path, script)
    logger.info("Box {} prepped: qemu+lima installed, {} ready, OS image staged", server_address, lima_service_user)


@server.command(name="list")
@click.option("--database-url", default=None, help="Pool DSN (else resolved from env/activated minds env).")
def list_servers(database_url: str | None) -> None:
    """List bare-metal servers with per-server and fleet slot accounting (from the DB)."""
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    logger.info("\n{}", _format_capacity_table(capacities))


@server.command(name="register")
@click.option("--ovh-service-name", required=True, help="OVH dedicated serviceName of the delivered box.")
@click.option("--plan-code", required=True, help="Catalog planCode the box was ordered as.")
@click.option("--region", required=True, help="OVH datacenter code (e.g. vin).")
@click.option("--public-address", required=True, help="SSH-reachable public address of the box.")
@click.option("--ram-gb", type=int, required=True, help="Total RAM in GB.")
@click.option("--cpu-cores", type=int, required=True, help="Physical CPU cores.")
@click.option("--cpu-threads", type=int, required=True, help="CPU threads.")
@click.option("--disk-gb", type=int, required=True, help="Usable disk in GB for slice data (split across slices).")
@click.option(
    "--memory-per-slice-gb",
    type=int,
    required=True,
    help="RAM (GB) each slice on this box advertises; sets slot count + per-slice sizing.",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor for sizing each slice's vCPUs.",
)
@click.option("--raid-level", default=None, help="RAID level configured at install (e.g. RAID1).")
@click.option("--lima-service-user", default="limahost", help="Non-root OS user that owns the box's lima VMs.")
@click.option("--ovh-order-id", default=None, help="OVH order id, if known.")
@click.option("--status", default=SERVER_STATUS_READY, help="Initial lifecycle status.")
@click.option("--database-url", default=None)
def register_server(
    ovh_service_name: str,
    plan_code: str,
    region: str,
    public_address: str,
    ram_gb: int,
    cpu_cores: int,
    cpu_threads: int,
    disk_gb: int,
    memory_per_slice_gb: int,
    cpu_overcommit: float,
    raid_level: str | None,
    lima_service_user: str,
    ovh_order_id: str | None,
    status: str,
    database_url: str | None,
) -> None:
    """Record an already-provisioned bare-metal box in the pool DB."""
    server_row = build_registered_server(
        ovh_service_name=ovh_service_name,
        plan_code=plan_code,
        region=region,
        public_address=public_address,
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit,
        raid_level=raid_level,
        lima_service_user=lima_service_user,
        ovh_order_id=ovh_order_id,
        status=status,
    )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        insert_bare_metal_server(conn, server_row)
    finally:
        conn.close()
    logger.info(
        "Registered bare-metal server {} ({}): {} slots, status {}",
        server_row.id,
        ovh_service_name,
        server_row.slot_count,
        status,
    )


def build_registered_server(
    *,
    ovh_service_name: str,
    plan_code: str,
    region: str,
    public_address: str,
    ram_gb: int,
    cpu_cores: int,
    cpu_threads: int,
    disk_gb: int,
    memory_per_slice_gb: int,
    cpu_overcommit_ratio: float,
    raid_level: str | None,
    lima_service_user: str,
    ovh_order_id: str | None,
    status: str,
) -> BareMetalServer:
    """Build a BareMetalServer from register inputs (slot count = floor(ram_gb / memory_per_slice_gb))."""
    now = datetime.now(timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        ovh_order_id=ovh_order_id,
        ovh_service_name=ovh_service_name,
        plan_code=plan_code,
        region=region,
        public_address=public_address,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_gb=ram_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit_ratio,
        slot_count=compute_slot_count(ram_gb, memory_per_slice_gb),
        raid_level=raid_level,
        lima_service_user=lima_service_user,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
    )


def compute_server_slice_sizing(server: BareMetalServer) -> dict[str, int]:
    """Compute the per-slice VM sizing for ``server`` from its stored inputs + specs.

    Returns ``{vcpus, memory_mib, disk_gib, advertised_memory_gb}`` -- identical for
    every slice on this box (so a single ``admin pool create --backend slice`` batch is one server).
    Raises ``BareMetalProvisioningError`` if the server is missing the inputs a
    pre-sizing registration would have set (re-register it first).
    """
    if (
        server.memory_per_slice_gb is None
        or server.cpu_overcommit_ratio is None
        or server.cpu_threads is None
        or server.disk_gb is None
        or server.slot_count <= 0
    ):
        raise BareMetalProvisioningError(
            f"server {server.id} is missing sizing inputs (memory_per_slice_gb / cpu_overcommit_ratio / "
            f"cpu_threads / disk_gb / slot_count); re-register it with the slice-sizing options"
        )
    return {
        "advertised_memory_gb": server.memory_per_slice_gb,
        "vcpus": compute_slice_vcpus(server.cpu_threads, server.slot_count, server.cpu_overcommit_ratio),
        "memory_mib": compute_slice_memory_mib(server.memory_per_slice_gb),
        "disk_gib": compute_slice_disk_gib(server.disk_gb, server.slot_count),
    }


def slice_advertised_attributes(sizing: dict[str, int]) -> dict[str, Any]:
    """The lease attributes a slice advertises (so a lease matches a slice or a VPS identically)."""
    return {"memory_gb": sizing["advertised_memory_gb"], "cpus": sizing["vcpus"]}


# Provider instance name the slice bake targets; -S overrides under this key
# carry the box address + per-slice carve sizing into the create.
_SLICE_PROVIDER_INSTANCE: str = "imbue_cloud_slice"


def _build_slice_create_args(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    region: str,
    pool_public_key: str,
    private_key_path: Path,
    ssh_user: str,
    port_range_start: int,
    port_range_end: int,
) -> list[str]:
    """Render the ``-S`` provider-config overrides that point one slice bake at this box.

    The carve knobs (vcpus / memory / disk) are computed per box so the leased
    host's actual size matches its advertised attributes; the box address + lima
    user + pool key + this bake's disjoint port window are passed the same way.
    Concurrent bakes get disjoint ``slice_port_range_*`` windows so their in-VM
    port probes cannot pick the same box ports.
    """
    prefix = f"providers.{_SLICE_PROVIDER_INSTANCE}"
    overrides = {
        "box_public_address": str(server.public_address),
        "box_ssh_user": ssh_user,
        "pool_private_key_path": str(private_key_path),
        "pool_authorized_public_key": pool_public_key,
        # Lease-region label (the app's region code, e.g. US-EAST-VA), NOT the
        # box's raw datacenter code -- so the connector's region-filtered lease
        # matches what the minds create form requests.
        "slice_region": region,
        "slice_vcpus": str(sizing["vcpus"]),
        "slice_memory_mib": str(sizing["memory_mib"]),
        "slice_disk_gib": str(sizing["disk_gib"]),
        "slice_port_range_start": str(port_range_start),
        "slice_port_range_end": str(port_range_end),
    }
    args: list[str] = []
    for key, value in overrides.items():
        args.extend(["-S", f"{prefix}.{key}={value}"])
    return args


def _rollback_slice_vm(*, server: BareMetalServer, ssh_user: str, private_key_path: Path, host_id: str) -> None:
    """Best-effort: destroy a carved slice VM whose later bake/bookkeeping failed, so it does not leak.

    Drives ``limactl delete`` / ``disk delete`` over SSH on the box (via the same
    SSH-backed client the carve uses) for the deterministic instance/disk names
    derived from ``host_id``. Swallows + logs any failure -- the caller is already
    on a failure path -- so it never masks the original error.
    """
    client = LimaSliceVpsClient(
        box_address=str(server.public_address),
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
    )
    instance_id = VpsInstanceId(slice_lima_instance_name(HostId(host_id)))
    try:
        client.destroy_instance(instance_id)
    except (MngrError, OSError) as exc:
        logger.warning("Rollback of orphaned slice VM for {} on {} failed: {}", host_id, server.public_address, exc)


def _slice_run_in_container(
    baked: BakedPoolHost, label: str, command: str, timeout_seconds: float
) -> tuple[int | None, str, str]:
    """Run a shell command inside a slice's container by SSHing the create-reported port.

    The :class:`~imbue.mngr_imbue_cloud.bake.pool_bake.ContainerCommandRunner` for
    slices: a slice's per-host forwarded port lives only in the create process's
    memory, so a fresh ``mngr`` can't resolve it -- instead we SSH straight to the
    container's box-forwarded port (``baked.ssh_port``) with the container key the
    create recorded. Wrapped in ``bash -lc`` so ``uv``/``mngr`` are on PATH in the
    FCT image. Returns ``(returncode, stdout, stderr)``.
    """
    if not baked.ssh_host or baked.ssh_port is None or not baked.ssh_key_path:
        return 1, "", f"baked slice {baked.host_name} missing container SSH connection info"
    ssh_command = [
        "ssh",
        "-i",
        baked.ssh_key_path,
        # Bake-time op to a container we just created, reached at a box-forwarded
        # port that earlier slices have reused with different host keys. Don't
        # consult or write the operator's shared known_hosts (a stale entry for this
        # box:port from a prior slice would fail strict checking and break the
        # teardown). Mirrors the OVH admin bake's container SSH (`_run_ssh_command`).
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=30",
        "-p",
        str(baked.ssh_port),
        f"{baked.ssh_user}@{baked.ssh_host}",
        f"bash -lc {shlex.quote(command)}",
    ]
    cg = ConcurrencyGroup(name=f"slice-container-{label}")
    with cg:
        result = cg.run_process_to_completion(command=ssh_command, timeout=timeout_seconds, is_checked_after=False)
    return result.returncode, result.stdout, result.stderr


def _bake_one_slice(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    lease_attributes: dict[str, Any],
    region: str,
    workspace_dir: Path,
    pool_public_key: str,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
    is_deferred_install_wait_skipped: bool,
) -> dict[str, Any]:
    """Bake one slice (laptop-driven ``mngr create`` against the slice provider) + insert its pool row.

    Returns an outcome dict (never raises). ``bake_pool_host`` carves the VM (over
    SSH on the box, inside the slice provider) and bakes the shared container; the
    shared :func:`finalize_baked_pool_host` then hardens the container sshd and
    tears down the bootstrap chat agent over the slice (direct-SSH) transport. Any
    failure once the VM exists rolls the VM back so it does not leak its box
    slot/ports (a ``mngr create`` failure is already rolled back by the provider).
    """
    ssh_user = server.lima_service_user or "limahost"
    host_name = f"slice-{uuid4().hex}"
    # The slice advertises the operator's lease attributes (e.g. repo_branch_or_tag,
    # so the minds fast-path lease matches) with the derived per-box size stamped on
    # top (authoritative). Mirrors how OVH pool hosts carry the operator's attributes.
    attributes = {**lease_attributes, **slice_advertised_attributes(sizing)}
    attributes_json = json.dumps(attributes)
    try:
        baked = bake_pool_host(
            provider_instance=_SLICE_PROVIDER_INSTANCE,
            host_name=host_name,
            attributes=attributes,
            workspace_dir=workspace_dir,
            extra_create_args=_build_slice_create_args(
                server=server,
                sizing=sizing,
                region=region,
                pool_public_key=pool_public_key,
                private_key_path=private_key_path,
                ssh_user=ssh_user,
                port_range_start=port_range_start,
                port_range_end=port_range_end,
            ),
        )
        # The VM now exists; any failure in the post-create steps or the insert must
        # tear it down so it does not leak its box slot + forwarded ports.
        try:
            if baked.outer_ssh_port is None or baked.ssh_port is None:
                raise BareMetalProvisioningError(
                    f"slice {host_name} create JSON missing the forwarded ports (vm={baked.outer_ssh_port}, "
                    f"container={baked.ssh_port})"
                )
            finalize_baked_pool_host(_slice_run_in_container, baked, host_name=host_name)
            # Let the FCT deferred-install (heavy apt + browser download) finish before we stop the
            # services agent: stopping mid-apt corrupts dpkg (see wait_for_deferred_install). Dev
            # bakes may skip this wait to save the few minutes; the tradeoff is the baked container's
            # deferred-install can be left incomplete/corrupt (acceptable for slow-path dev bakes,
            # whose container is rebuilt on lease anyway).
            if is_deferred_install_wait_skipped:
                logger.warning(
                    "Skipping deferred-install wait for slice {} (dev bake); its baked deferred-install may be incomplete",
                    host_name,
                )
            else:
                wait_for_deferred_install(_slice_run_in_container, baked, host_name=host_name)
            # Stop the services agent so it lands in the pool STOPPED, exactly like an
            # OVH pool host (which ``_create_single_pool_host`` stops via local mngr).
            # The fast-path lease then *starts* the adopted agent, which re-runs the
            # FCT bootstrap -- and because finalize removed the initial-chat sentinel,
            # the bootstrap re-creates the chat agent under the leasing user's
            # workspace name. Without this stop the agent stays running from bake
            # through lease, the one-shot bootstrap never re-runs, and the workspace
            # hangs at "Waiting for initial chat agent...". We stop it inside the
            # container (the operator's mngr can't resolve the slice's in-memory
            # forwarded ports, so the OVH local-stop approach can't be reused here).
            stop_rc, _stop_out, stop_err = _slice_run_in_container(
                baked, "stop-services", f"cd /mngr/code && uv run mngr stop {BAKED_SERVICES_AGENT_NAME}", 120.0
            )
            if stop_rc != 0:
                raise BareMetalProvisioningError(
                    f"stopping the services agent on slice {host_name} failed (exit {stop_rc}): {stop_err.strip()}"
                )
            host_id_obj = HostId(baked.host_id)
            values = build_slice_pool_host_insert_values(
                row_id=str(uuid4()),
                box_public_address=str(server.public_address),
                agent_id=baked.agent_id,
                host_id=baked.host_id,
                host_name=host_name,
                vm_ssh_host_port=baked.outer_ssh_port,
                container_ssh_host_port=baked.ssh_port,
                attributes_json=attributes_json,
                region=region,
                bare_metal_server_id=str(server.id),
                lima_instance_name=slice_lima_instance_name(host_id_obj),
                lima_disk_name=slice_lima_disk_name(host_id_obj),
            )
            conn = psycopg2.connect(database_url)
            try:
                insert_slice_pool_host(conn, values)
            finally:
                conn.close()
        except (PoolBakeError, BareMetalProvisioningError, MngrError, psycopg2.Error, OSError):
            _rollback_slice_vm(
                server=server, ssh_user=ssh_user, private_key_path=private_key_path, host_id=baked.host_id
            )
            raise
        logger.info(
            "Slice {} ready on {} (host_id={}, ports vm={}/container={})",
            host_name,
            server.public_address,
            baked.host_id,
            baked.outer_ssh_port,
            baked.ssh_port,
        )
        return {
            "host_name": host_name,
            "server_id": str(server.id),
            "host_id": baked.host_id,
            "agent_id": baked.agent_id,
            "vm_ssh_port": baked.outer_ssh_port,
            "container_ssh_port": baked.ssh_port,
            "attributes": attributes,
            "status": "succeeded",
        }
    except (PoolBakeError, BareMetalProvisioningError, MngrError, psycopg2.Error, OSError) as exc:
        logger.warning("Slice bake {} failed: {}", host_name, exc)
        return {"host_name": host_name, "server_id": str(server.id), "status": "failed", "error": str(exc)}


def _bake_into_outcomes(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    lease_attributes: dict[str, Any],
    region: str,
    workspace_dir: Path,
    pool_public_key: str,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
    is_deferred_install_wait_skipped: bool,
    outcomes: list[dict[str, Any]],
    outcomes_lock: "threading.Lock",
) -> None:
    """Thread target: bake one slice and append its outcome under the lock."""
    outcome = _bake_one_slice(
        server=server,
        sizing=sizing,
        lease_attributes=lease_attributes,
        region=region,
        workspace_dir=workspace_dir,
        pool_public_key=pool_public_key,
        private_key_path=private_key_path,
        database_url=database_url,
        port_range_start=port_range_start,
        port_range_end=port_range_end,
        is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
    )
    with outcomes_lock:
        outcomes.append(outcome)


def allocate_slices(
    *,
    count: int,
    lease_attributes: dict[str, Any],
    region: str,
    workspace_dir: Path,
    mngr_source: str | None,
    database_url: str,
    is_dry_run: bool,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Bake ``count`` slices onto a single ready bare-metal server and insert their pool rows.

    The slice backend of ``admin pool create``. Picks the ready server with the
    most free slots (one server per invocation: a server's per-slice vCPU/RAM/disk
    are fixed by its registration, so a batch is homogeneous), vendors this branch's
    mngr into the FCT workspace once, then bakes the slices in parallel -- each
    ``mngr create`` drives the slice provider, which carves a lima VM over SSH on
    the box and bakes the shared container, exactly like an OVH pool bake. Each row
    advertises ``lease_attributes`` (the operator's lease metadata) with the derived
    per-box size stamped on top, and records ``region`` (the lease-region label, not
    the box's raw datacenter code) so the connector's region-filtered lease matches.
    ``database_url`` is already resolved by the caller. ``is_dry_run`` only reports
    placement.
    """
    if count <= 0:
        raise click.UsageError("--count must be positive")
    conn = psycopg2.connect(database_url)
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    # One server per batch (homogeneous sizing): pick the ready box with the most
    # free slots and require it to hold the whole batch. Server selection is NOT
    # filtered by ``region`` today (single-fleet assumption); multi-region fleet
    # filtering is future work.
    chosen = choose_server_for_new_slice(capacities)
    server = chosen.server
    if chosen.free_slots < count:
        raise click.UsageError(
            f"server {server.id} has only {chosen.free_slots} free slot(s); cannot bake {count} "
            "(allocate on one server per invocation -- run again to use another server)"
        )
    sizing = compute_server_slice_sizing(server)

    if is_dry_run:
        emit_json(
            {
                "dry_run": True,
                "server_id": str(server.id),
                "public_address": server.public_address,
                "region": region,
                "count": count,
                "free_slots": chosen.free_slots,
                "per_slice_sizing": sizing,
                "attributes": {**lease_attributes, **slice_advertised_attributes(sizing)},
            }
        )
        return

    # Resolve the mngr source tree (default to this checkout). The workspace dir
    # is already resolved + validated by the caller's bake-source context.
    repo_root = Path(__file__).resolve().parents[5]
    resolved_mngr_source = Path(mngr_source) if mngr_source else repo_root
    if not server.public_address:
        raise click.UsageError(f"server {server.id} has no public_address; cannot bake")

    # Vendor this branch's mngr into the FCT workspace once (the baked container
    # builds its mngr from vendor/mngr); the parallel bakes then share it.
    sync_mngr_into_template(resolved_mngr_source, workspace_dir)

    with _pool_private_key_path() as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        # Bake all slices in parallel (one thread each). Each bake is a separate
        # ``mngr create`` that drives the slice provider to carve a VM (over SSH on
        # the box) and pick the lowest free ports in the window it is given; handing
        # each a DISJOINT sub-range of the box port range stops concurrent bakes
        # from deterministically choosing the same ports.
        outcomes: list[dict[str, Any]] = []
        outcomes_lock = threading.Lock()
        port_windows = [
            partition_port_range(DEFAULT_SLICE_PORT_RANGE_START, DEFAULT_SLICE_PORT_RANGE_END, count, idx)
            for idx in range(count)
        ]
        threads = [
            ObservableThread(
                target=_bake_into_outcomes,
                kwargs=dict(
                    server=server,
                    sizing=sizing,
                    lease_attributes=lease_attributes,
                    region=region,
                    workspace_dir=workspace_dir,
                    pool_public_key=pool_public_key,
                    private_key_path=private_key_path,
                    database_url=database_url,
                    port_range_start=port_windows[idx][0],
                    port_range_end=port_windows[idx][1],
                    is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
                    outcomes=outcomes,
                    outcomes_lock=outcomes_lock,
                ),
                name=f"bake-{idx}",
            )
            for idx in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    succeeded = [outcome for outcome in outcomes if outcome.get("status") == "succeeded"]
    emit_json(
        {
            "requested": count,
            "succeeded": len(succeeded),
            "failed": count - len(succeeded),
            "slices": outcomes,
        }
    )
    if len(succeeded) < count:
        raise SystemExit(1)


@server.command(name="set-status")
@click.option("--server-id", required=True, help="bare_metal_servers row id.")
@click.option("--status", required=True, help="New lifecycle status.")
@click.option("--database-url", default=None)
def set_status(server_id: str, status: str, database_url: str | None) -> None:
    """Advance a server's lifecycle status (resumable order->delivered->installing->ready)."""
    validated = BareMetalServerStatus(status)
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        update_server(conn, BareMetalServerDbId(server_id), status=str(validated))
    finally:
        conn.close()
    logger.info("Set server {} status to {}", server_id, validated)


def _format_delivery(delivery_hours: int) -> str:
    """Human-readable delivery time from OVH availability hours (e.g. 1 -> '~1h', 72 -> '3d')."""
    if delivery_hours <= 0:
        return "?"
    if delivery_hours < 24:
        return f"~{delivery_hours}h"
    return f"{delivery_hours // 24}d"


def _format_storage_options(row: SlicePricingRow) -> str:
    """Render a row's storage upgrade options as a compact end-of-row string."""
    if not row.storage_options:
        return "-"
    return "  ".join(
        f"{option.label}(+{option.extra_disk_gb_per_slice}G/slice @ ${option.dollars_per_extra_gb}/GB)"
        for option in row.storage_options
    )


def _format_slice_pricing_table(rows: list[SlicePricingRow]) -> str:
    """Render the per-slice pricing rows as a plain table (already sorted cheapest-per-slice first)."""
    headers = [
        "$/SLICE/MO",
        "PLAN_CODE",
        "MODEL",
        "REGION",
        "DELIVERY",
        "STOCK",
        "RAM_GB",
        "SLOTS",
        "CPU(c/t)",
        "CPU/SLICE",
        "DISK/SLICE(GiB)",
        "$/MO",
        "SETUP",
        "BASE_STORAGE",
        "STORAGE_UPGRADES (per slice)",
    ]
    table_rows = [
        [
            f"{row.price_per_slice_usd:.2f}",
            row.plan_code,
            row.server_model,
            row.region,
            _format_delivery(row.delivery_hours),
            row.stock_level or "-",
            row.server_ram_gb,
            row.slot_count,
            f"{row.cpu_cores}c/{row.cpu_threads}t",
            row.cpus_per_slice,
            row.disk_gb_per_slice,
            f"{row.recurring_monthly_usd:.2f}",
            f"{row.one_time_setup_usd:.2f}",
            row.base_storage_label,
            _format_storage_options(row),
        ]
        for row in rows
    ]
    return tabulate(table_rows, headers=headers, tablefmt="plain")


@server.command(name="pricing")
@click.option(
    "--region",
    "regions",
    type=click.Choice(sorted(OVH_US_DATACENTER_CODES)),
    multiple=True,
    help="Restrict to a US datacenter (vin=US-EAST-VA, hil=US-WEST-OR). Repeatable; default: both.",
)
@click.option(
    "--memory-per-slice-gb",
    type=int,
    default=DEFAULT_MEMORY_PER_SLICE_GB,
    show_default=True,
    help="RAM (GB) per slice; sets slot count (floor(server_RAM / this)) and per-slice CPU/disk sizing.",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor for sizing each slice's vCPUs.",
)
@click.option(
    "--catalog-name",
    default="eco",
    show_default=True,
    help="OVH catalog to price (eco = the RISE/SYS/KS bare-metal line we carve slices on).",
)
def pricing(regions: tuple[str, ...], memory_per_slice_gb: int, cpu_overcommit: float, catalog_name: str) -> None:
    """Print a per-slice pricing table for OVH bare-metal plans (read-only; needs OVH_* creds in env).

    Each row is a server x RAM config; price/slice = (month-to-month + setup/12) / slots, sorted cheapest
    first, with delivery time + stock from OVH availability and storage-upgrade options at the end of each
    row. This only reads the catalog/availability APIs -- it never places an order.
    """
    config = OvhProviderConfig()
    if not config.has_explicit_credentials():
        raise BareMetalProvisioningError(
            "No OVH credentials found. Export OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY "
            "(from the activated env's ovh secret) before running pricing."
        )
    allowed_regions = frozenset(regions) if regions else OVH_US_DATACENTER_CODES

    client = build_ovh_client(config)
    # The OVH SDK's generic call() sends kwargs as the request body, so for GETs the query params must
    # go in the path; the availabilities endpoint takes no params here (we fetch all and filter locally).
    catalog_path = f"/order/catalog/public/{catalog_name}?{urlencode({'ovhSubsidiary': client.subsidiary})}"
    catalog = client.call_api("GET", catalog_path)
    availabilities = client.call_api("GET", "/dedicated/server/datacenter/availabilities")
    rows = compute_slice_pricing_rows(catalog, availabilities, allowed_regions, memory_per_slice_gb, cpu_overcommit)

    region_label = ",".join(sorted(allowed_regions))
    if not rows:
        write_human_line(f"No orderable plans found in region(s) {region_label} at {memory_per_slice_gb}GB/slice.")
        return
    header = (
        f"OVH bare-metal slice pricing -- {memory_per_slice_gb}GB/slice, "
        f"{cpu_overcommit}x CPU overcommit, region(s) {region_label} (catalog '{catalog_name}')"
    )
    write_human_line(f"{header}\n{_format_slice_pricing_table(rows)}")


def _require_ovh_config() -> OvhProviderConfig:
    """Return the OVH provider config, raising a clear error if no credentials are present in the env."""
    config = OvhProviderConfig()
    if not config.has_explicit_credentials():
        raise BareMetalProvisioningError(
            "No OVH credentials found. Export OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY "
            "(from the activated env's ovh secret) first."
        )
    return config


def _probe_ssh_ready(server_address: str, ssh_user: str, private_key_path: Path) -> bool | None:
    """One SSH-readiness probe: True once a login succeeds, else None (for poll_for_value)."""
    cg = ConcurrencyGroup(name="ssh-ready")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                "ssh",
                "-i",
                str(private_key_path),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=15",
                f"{ssh_user}@{server_address}",
                "echo ok",
            ],
            timeout=30.0,
            is_checked_after=False,
        )
    return True if result.returncode == 0 else None


def _wait_for_ssh_ready(server_address: str, ssh_user: str, private_key_path: Path, timeout_seconds: float) -> None:
    """Poll until the box accepts an SSH login (it reboots into the freshly-installed OS). Raises on timeout."""
    with log_span("Waiting for SSH on {} as {}", server_address, ssh_user):
        is_ready, _polls, _elapsed = poll_for_value(
            lambda: _probe_ssh_ready(server_address, ssh_user, private_key_path),
            timeout=timeout_seconds,
            poll_interval=10.0,
        )
    if not is_ready:
        raise BareMetalProvisioningError(f"SSH to {server_address} not ready within {timeout_seconds:.0f}s")


@server.command(name="order")
@click.option("--plan-code", required=True, help="OVH eco planCode to order (e.g. 24rise01-v1-us).")
@click.option(
    "--region",
    required=True,
    type=click.Choice(sorted(OVH_US_DATACENTER_CODES)),
    help="OVH US datacenter to order in (vin = US-EAST-VA, hil = US-WEST-OR).",
)
@click.option("--memory-gb", required=True, type=int, help="Server RAM in GB (selects the memory option).")
@click.option(
    "--storage",
    required=True,
    help="Storage option short code (the pricing table's BASE_STORAGE, e.g. softraid-2x512nvme).",
)
@click.option(
    "--memory-per-slice-gb",
    type=int,
    default=DEFAULT_MEMORY_PER_SLICE_GB,
    show_default=True,
    help="RAM (GB) each slice will advertise; sets slot_count = floor(server RAM / this).",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor recorded for slice sizing on this box.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the interactive confirmation and place the order.")
@click.option("--database-url", default=None, help="Pool DSN (else resolved from env/activated minds env).")
def order(
    plan_code: str,
    region: str,
    memory_gb: int,
    storage: str,
    memory_per_slice_gb: int,
    cpu_overcommit: float,
    yes: bool,
    database_url: str | None,
) -> None:
    """Order a bare-metal server from OVH (THIS CHARGES the account) and record it at status 'ordered'.

    Builds + assigns the eco cart, shows the real OVH price preview for confirmation, places the order, and
    inserts a bare_metal_servers row (specs derived from the catalog). Then run ``await-delivery`` + ``setup``.
    Needs OVH_* credentials and the pool DSN.
    """
    config = _require_ovh_config()
    client = build_ovh_client(config)
    catalog_path = f"/order/catalog/public/eco?{urlencode({'ovhSubsidiary': client.subsidiary})}"
    catalog = client.call_api("GET", catalog_path)
    cpu_cores, cpu_threads, disk_gb, raid_level = derive_server_specs(catalog, plan_code, storage)
    slot_count = compute_slot_count(memory_gb, memory_per_slice_gb)
    if slot_count <= 0:
        raise BareMetalProvisioningError(
            f"{memory_gb}GB RAM / {memory_per_slice_gb}GB per slice yields 0 slots; pick a smaller slice size"
        )

    cart_id, preview, _option_codes = build_and_assign_eco_cart(
        client, plan_code=plan_code, datacenter=region, memory_gb=memory_gb, storage_short=storage
    )
    write_human_line(
        f"About to order {plan_code} in {region}: {memory_gb}GB RAM, {storage}, {cpu_cores}c/{cpu_threads}t, "
        f"{disk_gb}GB usable disk ({raid_level}) -> {slot_count} slices of {memory_per_slice_gb}GB.\n"
        f"OVH price preview:\n{summarize_checkout_prices(preview)}"
    )
    if not yes and not click.confirm("Place this order now (this charges the account)?", default=False):
        delete_cart_quietly(client, cart_id)
        write_human_line("Aborted; cart deleted, no order placed.")
        return

    order_id = checkout_eco_cart(client, cart_id)
    now = datetime.now(timezone.utc)
    server_row = BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        ovh_order_id=str(order_id),
        ovh_service_name=None,
        plan_code=plan_code,
        region=region,
        public_address=None,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_gb=memory_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit,
        slot_count=slot_count,
        raid_level=raid_level,
        lima_service_user=None,
        status=BareMetalServerStatus(SERVER_STATUS_ORDERED),
        created_at=now,
        updated_at=now,
    )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        insert_bare_metal_server(conn, server_row)
    finally:
        conn.close()
    write_human_line(
        f"Ordered {plan_code} (OVH order {order_id}); recorded server {server_row.id} at status 'ordered'. "
        f"Next: `admin server await-delivery --server-id {server_row.id}`."
    )


def _fetch_server_or_raise(dsn: str, server_id: str) -> BareMetalServer:
    """Read one server row with a short-lived connection (never held across a long OVH/SSH wait)."""
    conn = psycopg2.connect(dsn)
    try:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    finally:
        conn.close()
    if server is None:
        raise BareMetalProvisioningError(f"no bare_metal_servers row with id {server_id}")
    return server


def _update_server_fields(dsn: str, server_id: str, **fields: Any) -> None:
    """Update a server row with a short-lived connection (Neon drops connections idle across a long wait)."""
    conn = psycopg2.connect(dsn)
    try:
        update_server(conn, BareMetalServerDbId(server_id), **fields)
    finally:
        conn.close()


@server.command(name="await-delivery")
@click.option("--server-id", required=True, help="bare_metal_servers row id (from `order`).")
@click.option("--database-url", default=None)
def await_delivery(server_id: str, database_url: str | None) -> None:
    """Wait for OVH to deliver an ordered server (assign a serviceName + IP), then mark it 'delivered'.

    Resumable: a no-op if the server is already delivered. Delivery can take a while (often ~1h).
    """
    dsn = resolve_pool_database_url(database_url)
    server = _fetch_server_or_raise(dsn, server_id)
    if str(server.status) in (SERVER_STATUS_DELIVERED, SERVER_STATUS_INSTALLING, SERVER_STATUS_READY):
        write_human_line(f"Already delivered: {server.ovh_service_name} ({server.public_address}).")
        return
    if not server.ovh_order_id:
        raise BareMetalProvisioningError(f"server {server_id} has no ovh_order_id to wait on")
    # Resolve serviceName + IP without holding the DB connection (delivery polling can run for ~1h).
    client = build_ovh_client(_require_ovh_config())
    service_name = wait_for_order_service_name(client, order_id=int(server.ovh_order_id))
    address = wait_for_dedicated_server_address(client, service_name=service_name)
    _update_server_fields(
        dsn,
        server_id,
        ovh_service_name=service_name,
        public_address=address,
        status=SERVER_STATUS_DELIVERED,
    )
    write_human_line(
        f"Server {server_id} delivered: {service_name} ({address}). "
        f"Next: `admin server setup --server-id {server_id}`."
    )


@server.command(name="setup")
@click.option("--server-id", required=True, help="bare_metal_servers row id (delivered).")
@click.option("--ssh-user", default="debian", help="Bootstrap SSH user after reinstall (OS image's default user).")
@click.option("--lima-service-user", default="limahost", help="Dedicated non-root user to create for the lima VMs.")
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box.")
@click.option(
    "--slice-base-image-url",
    default=DEFAULT_IMAGE_URL_X86_64,
    show_default=True,
    help="Guest OS image to stage on the box once (slices boot from this via file://).",
)
@click.option(
    "--os-template",
    default=DEFAULT_REINSTALL_OS_TEMPLATE,
    show_default=True,
    help="OVH OS template to reinstall onto the box.",
)
@click.option("--ssh-ready-timeout", type=float, default=900.0, show_default=True, help="Seconds to wait for SSH.")
@click.option("--database-url", default=None)
def setup(
    server_id: str,
    ssh_user: str,
    lima_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
    os_template: str,
    ssh_ready_timeout: float,
    database_url: str | None,
) -> None:
    """Provision a delivered box to 'ready': reinstall our OS (destructive), prep qemu/lima/tooling, stage image.

    Resumable via status: reinstall runs only from 'delivered'; re-running from 'installing' resumes at prep.
    """
    dsn = resolve_pool_database_url(database_url)
    server = _fetch_server_or_raise(dsn, server_id)
    if str(server.status) == SERVER_STATUS_READY:
        write_human_line(f"Server {server_id} is already ready ({server.ovh_service_name}).")
        return
    if str(server.status) not in (SERVER_STATUS_DELIVERED, SERVER_STATUS_INSTALLING):
        raise BareMetalProvisioningError(
            f"server {server_id} is {server.status}; run `await-delivery` until it is 'delivered' first"
        )
    service_name = server.ovh_service_name
    address = server.public_address
    if not service_name or not address:
        raise BareMetalProvisioningError(f"server {server_id} has no serviceName/address; re-run await-delivery")

    client = build_ovh_client(_require_ovh_config())
    with _pool_private_key_path() as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        # Reinstall only from 'delivered'; re-running from 'installing' assumes the reinstall completed and
        # resumes at SSH-wait + prep. No DB connection is held across the (long) reinstall/prep waits.
        if str(server.status) == SERVER_STATUS_DELIVERED:
            task_id = start_os_reinstall(
                client,
                service_name=service_name,
                ssh_public_key=pool_public_key,
                os_template=os_template,
            )
            _update_server_fields(dsn, server_id, status=SERVER_STATUS_INSTALLING)
            wait_for_os_reinstall(client, service_name=service_name, task_id=task_id)

        _wait_for_ssh_ready(address, ssh_user, private_key_path, ssh_ready_timeout)
        script = build_box_prep_script(
            pool_public_key=pool_public_key,
            lima_service_user=lima_service_user,
            lima_version=lima_version,
            slice_base_image_url=slice_base_image_url,
        )
        logger.info("Prepping delivered box {} ({})", server_id, address)
        _run_root_script_over_ssh(address, ssh_user, private_key_path, script)

    _update_server_fields(dsn, server_id, lima_service_user=lima_service_user, status=SERVER_STATUS_READY)
    write_human_line(
        f"Server {server_id} is READY: {service_name} ({address}), "
        f"{server.slot_count} slots. Bake a slice with `admin pool create --backend slice`."
    )
