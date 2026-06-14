"""``mngr imbue_cloud admin server ...`` -- operator-only bare-metal server + slice management.

Manages the OVH bare-metal servers we rent and the lima-VM "slices" we carve on
them, as an alternative to ordering OVH VPSes. Writes the connector's host_pool
Neon DB directly (laptop-side), mirroring ``admin pool create``; the connector
only reads these rows (plus its release-time teardown). Every step is resumable:
ordering and OS install can take a long time, and re-running advances a box one
step. The OVH-touching steps (order / install / destroy) act on the real account
and are validated against a delivered box; ``list`` / ``register`` / ``allocate``
are exercised without OVH.
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
from uuid import uuid4

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ObservableThread
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.bare_metal import choose_server_for_new_slice
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.bare_metal import partition_port_range
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.bare_metal_db import build_slice_pool_host_insert_values
from imbue.mngr_imbue_cloud.bare_metal_db import fetch_server_capacities
from imbue.mngr_imbue_cloud.bare_metal_db import insert_bare_metal_server
from imbue.mngr_imbue_cloud.bare_metal_db import insert_slice_pool_host
from imbue.mngr_imbue_cloud.bare_metal_db import update_server
from imbue.mngr_imbue_cloud.bare_metal_prep import DEFAULT_LIMA_VERSION
from imbue.mngr_imbue_cloud.bare_metal_prep import build_box_prep_script
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli.admin import resolve_pool_database_url
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.pool_bake import BakedPoolHost
from imbue.mngr_imbue_cloud.pool_bake import PoolBakeError
from imbue.mngr_imbue_cloud.pool_bake import bake_pool_host
from imbue.mngr_imbue_cloud.pool_bake import finalize_baked_pool_host
from imbue.mngr_imbue_cloud.pool_bake import sync_mngr_into_template
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
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
    """Bare-metal server + slice management (OVH + Neon)."""


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
def prep_box(server_address: str, ssh_user: str, lima_service_user: str, lima_version: str) -> None:
    """Install QEMU + lima + tooling on a delivered box and create the lima service user.

    Idempotent. Authorizes the pool management key (POOL_SSH_PRIVATE_KEY) for the
    service user so the admin CLI can bake slices and the connector can tear them
    down. Run after the OS install, before ``allocate-slice``.
    """
    with _pool_private_key_path() as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        script = build_box_prep_script(
            pool_public_key=pool_public_key,
            lima_service_user=lima_service_user,
            lima_version=lima_version,
        )
        logger.info(
            "Prepping box {} as {} (lima user {}, lima {})", server_address, ssh_user, lima_service_user, lima_version
        )
        _run_root_script_over_ssh(server_address, ssh_user, private_key_path, script)
    logger.info("Box {} prepped: qemu+lima installed, {} ready", server_address, lima_service_user)


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
    every slice on this box (so a single ``allocate-slice`` batch is one server).
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


# Default FCT workspace checkout to bake from when --workspace-dir is omitted.
_DEFAULT_FCT_WORKSPACE: Path = Path.home() / "project" / "forever-claude-template"
# Provider instance name the slice bake targets; -S overrides under this key
# carry the box address + per-slice carve sizing into the create.
_SLICE_PROVIDER_INSTANCE: str = "imbue_cloud_slice"


def _build_slice_create_args(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
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
        "slice_region": server.region,
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

    The :class:`~imbue.mngr_imbue_cloud.pool_bake.ContainerCommandRunner` for
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
    workspace_dir: Path,
    pool_public_key: str,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
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
    attributes = slice_advertised_attributes(sizing)
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
                region=server.region,
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
    workspace_dir: Path,
    pool_public_key: str,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
    outcomes: list[dict[str, Any]],
    outcomes_lock: "threading.Lock",
) -> None:
    """Thread target: bake one slice and append its outcome under the lock."""
    outcome = _bake_one_slice(
        server=server,
        sizing=sizing,
        workspace_dir=workspace_dir,
        pool_public_key=pool_public_key,
        private_key_path=private_key_path,
        database_url=database_url,
        port_range_start=port_range_start,
        port_range_end=port_range_end,
    )
    with outcomes_lock:
        outcomes.append(outcome)


@server.command(name="allocate-slice")
@click.option("--count", type=int, default=1, help="Number of slices to bake on the chosen server.")
@click.option(
    "--workspace-dir",
    type=click.Path(exists=True),
    default=None,
    help="forever-claude-template checkout to bake from (default: $HOME/project/forever-claude-template).",
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="mngr monorepo root to vendor into the FCT workspace (default: this checkout).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Report placement + slice sizing; do not bake.")
@click.option("--database-url", default=None)
def allocate_slice(
    count: int,
    workspace_dir: str | None,
    mngr_source: str | None,
    dry_run: bool,
    database_url: str | None,
) -> None:
    """Bake ``--count`` slices onto a single ready bare-metal server and insert their pool rows.

    Picks the ready server with the most free slots (one server per invocation: a
    server's per-slice vCPU/RAM/disk are fixed by its registration, so a batch is
    homogeneous), vendors this branch's mngr into the FCT workspace once, then
    bakes the slices in parallel from here -- each ``mngr create`` drives the slice
    provider, which carves a lima VM over SSH on the box and bakes the shared
    container, exactly like an OVH pool bake. Each bake authorizes the pool key,
    tears down the bootstrap chat agent, and inserts an ``available`` slice
    ``pool_hosts`` row. The per-slice sizing comes from the server's stored
    memory-per-slice + CPU overcommit + disk. ``--dry-run`` only reports placement.
    """
    if count <= 0:
        raise click.UsageError("--count must be positive")
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    # One server per batch (homogeneous sizing): pick the ready box with the most
    # free slots and require it to hold the whole batch.
    chosen = choose_server_for_new_slice(capacities)
    server = chosen.server
    if chosen.free_slots < count:
        raise click.UsageError(
            f"server {server.id} has only {chosen.free_slots} free slot(s); cannot bake {count} "
            "(allocate on one server per invocation -- run again to use another server)"
        )
    sizing = compute_server_slice_sizing(server)

    if dry_run:
        emit_json(
            {
                "dry_run": True,
                "server_id": str(server.id),
                "public_address": server.public_address,
                "region": server.region,
                "count": count,
                "free_slots": chosen.free_slots,
                "per_slice_sizing": sizing,
                "attributes": slice_advertised_attributes(sizing),
            }
        )
        return

    # Resolve the source trees (default to this checkout for mngr, and the
    # conventional FCT checkout for the workspace).
    repo_root = Path(__file__).resolve().parents[5]
    resolved_mngr_source = Path(mngr_source) if mngr_source else repo_root
    resolved_workspace_dir = Path(workspace_dir) if workspace_dir else _DEFAULT_FCT_WORKSPACE
    if not resolved_workspace_dir.is_dir():
        raise click.UsageError(
            f"FCT workspace not found at {resolved_workspace_dir}; pass --workspace-dir explicitly."
        )
    if not server.public_address:
        raise click.UsageError(f"server {server.id} has no public_address; cannot bake")

    # Vendor this branch's mngr into the FCT workspace once (the baked container
    # builds its mngr from vendor/mngr); the parallel bakes then share it.
    sync_mngr_into_template(resolved_mngr_source, resolved_workspace_dir)

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
                    workspace_dir=resolved_workspace_dir,
                    pool_public_key=pool_public_key,
                    private_key_path=private_key_path,
                    database_url=resolved_database_url,
                    port_range_start=port_windows[idx][0],
                    port_range_end=port_windows[idx][1],
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
