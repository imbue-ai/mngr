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
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_FALLBACK_CPU_THREADS
from imbue.mngr_imbue_cloud.bare_metal import SLICE_ADVERTISED_RAM_GB
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.bare_metal import plan_slice_placements
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
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slice_bake import build_chat_teardown_container_command
from imbue.mngr_imbue_cloud.slice_bake import build_slice_bake_remote_command
from imbue.mngr_imbue_cloud.slice_bake import build_wait_for_sentinel_container_command
from imbue.mngr_imbue_cloud.slice_bake import parse_create_json_from_output


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
@click.option("--ram-gb", type=int, required=True, help="Total RAM in GB (drives slot count).")
@click.option("--cpu-cores", type=int, required=True, help="Physical CPU cores.")
@click.option("--cpu-threads", type=int, required=True, help="CPU threads.")
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
    raid_level: str | None,
    lima_service_user: str,
    ovh_order_id: str | None,
    status: str,
) -> BareMetalServer:
    """Build a BareMetalServer from register-command inputs (slot count derived from RAM)."""
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
        slot_count=compute_slot_count(ram_gb),
        raid_level=raid_level,
        lima_service_user=lima_service_user,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
    )


def plan_next_slice_attributes(capacity: BareMetalServerCapacity, overcommit_ratio: float) -> dict[str, Any]:
    """Compute the lease attributes a new slice on ``capacity``'s server advertises.

    Every slice advertises 8 GB and a vCPU count derived from the server's threads
    with mild overcommit, so a lease matches a slice or a VPS identically.
    """
    server = capacity.server
    threads = server.cpu_threads or DEFAULT_SLICE_FALLBACK_CPU_THREADS
    slot_count = server.slot_count or 1
    return {
        "memory_gb": SLICE_ADVERTISED_RAM_GB,
        "cpus": compute_slice_vcpus(threads, slot_count, overcommit_ratio),
    }


# Default on-box layout the bake assumes (created by the sync step).
_BOX_MNGR_REMOTE_NAME: str = "mngr"
_BOX_FCT_REMOTE_NAME: str = "forever-claude-template"
# rsync excludes layered on top of ``--filter=:- .gitignore`` (mirrors the minds
# vendor-sync / admin pool create rsync). ``.external_worktrees`` is gitignored
# so it is already covered, but kept explicit for the FCT sync below.
_RSYNC_MANUAL_EXCLUDES: tuple[str, ...] = (".git", "uv.lock", ".external_worktrees")
_UV_SYNC_TIMEOUT_SECONDS: float = 900.0
_SLICE_BAKE_TIMEOUT_SECONDS: float = 2700.0
_BOX_SSH_TIMEOUT_SECONDS: float = 120.0
_SENTINEL_POLL_TIMEOUT_SECONDS: float = 480.0


def _remote_home(ssh_user: str) -> str:
    return f"/home/{ssh_user}"


def _ssh_transport_args(private_key_path: Path) -> list[str]:
    return [
        "-i",
        str(private_key_path),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=30",
    ]


def _run_remote(
    *,
    address: str,
    ssh_user: str,
    private_key_path: Path,
    port: int,
    command: str,
    timeout_seconds: float,
    label: str,
    is_streaming: bool,
) -> Any:
    """Run a command over SSH and return the FinishedProcess (does not raise on non-zero)."""
    ssh_command = [
        "ssh",
        *_ssh_transport_args(private_key_path),
        "-p",
        str(port),
        f"{ssh_user}@{address}",
        command,
    ]
    on_output = (lambda line, _is_stdout: logger.info("  [{}] {}", label, line.rstrip())) if is_streaming else None
    cg = ConcurrencyGroup(name=f"slice-{label}")
    with cg:
        return cg.run_process_to_completion(
            command=ssh_command,
            timeout=timeout_seconds,
            is_checked_after=False,
            on_output=on_output,
        )


def _rsync_dir_to_box(
    *,
    local_dir: Path,
    remote_dest: str,
    address: str,
    ssh_user: str,
    private_key_path: Path,
    extra_excludes: tuple[str, ...],
) -> None:
    """Rsync ``local_dir`` to ``ssh_user@address:remote_dest`` (gitignore-filtered)."""
    exclude_args: list[str] = []
    for pattern in extra_excludes:
        exclude_args.extend(["--exclude", pattern])
    ssh_transport = "ssh " + " ".join(_ssh_transport_args(private_key_path))
    command = [
        "rsync",
        "-a",
        "--delete",
        "--filter=:- .gitignore",
        *exclude_args,
        "-e",
        ssh_transport,
        f"{local_dir}/",
        f"{ssh_user}@{address}:{remote_dest}/",
    ]
    cg = ConcurrencyGroup(name="slice-rsync")
    with cg:
        result = cg.run_process_to_completion(command=command, timeout=600.0, is_checked_after=False)
    if result.returncode != 0:
        raise BareMetalProvisioningError(
            f"rsync of {local_dir} to {address}:{remote_dest} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _sync_repos_to_box(
    *,
    mngr_source: Path,
    workspace_dir: Path,
    address: str,
    ssh_user: str,
    private_key_path: Path,
) -> None:
    """Put this branch's mngr + the FCT workspace on the box, ready to bake.

    Idempotent. Rsyncs the monorepo to ``~/mngr`` and ``uv sync --all-packages``
    there (so ``~/mngr/.venv/bin/mngr`` has the slice provider), rsyncs the FCT
    workspace to ``~/forever-claude-template`` (minus its own vendored mngr), then
    copies the just-synced monorepo into the FCT workspace's ``vendor/mngr`` so the
    baked container's mngr matches this branch.
    """
    home = _remote_home(ssh_user)
    logger.info("Syncing mngr -> {}:{}/{}", address, home, _BOX_MNGR_REMOTE_NAME)
    _rsync_dir_to_box(
        local_dir=mngr_source,
        remote_dest=_BOX_MNGR_REMOTE_NAME,
        address=address,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        extra_excludes=_RSYNC_MANUAL_EXCLUDES,
    )
    logger.info("Running uv sync --all-packages on {}", address)
    uv_result = _run_remote(
        address=address,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        port=22,
        command=f"cd {_BOX_MNGR_REMOTE_NAME} && $HOME/.local/bin/uv sync --all-packages",
        timeout_seconds=_UV_SYNC_TIMEOUT_SECONDS,
        label="uv-sync",
        is_streaming=True,
    )
    if uv_result.returncode != 0:
        raise BareMetalProvisioningError(f"uv sync on {address} failed: {uv_result.stderr.strip()}")
    logger.info("Syncing FCT workspace -> {}:{}/{}", address, home, _BOX_FCT_REMOTE_NAME)
    _rsync_dir_to_box(
        local_dir=workspace_dir,
        remote_dest=_BOX_FCT_REMOTE_NAME,
        address=address,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        # Skip the FCT's own vendored mngr; we overwrite it with the monorepo next.
        extra_excludes=(*_RSYNC_MANUAL_EXCLUDES, "vendor/mngr"),
    )
    logger.info("Refreshing {}:{}/vendor/mngr from the synced monorepo", address, _BOX_FCT_REMOTE_NAME)
    vendor_sync = _run_remote(
        address=address,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        port=22,
        command=(
            f"mkdir -p {_BOX_FCT_REMOTE_NAME}/vendor/mngr && "
            f"rsync -a --delete --exclude .venv --exclude .git "
            f"{home}/{_BOX_MNGR_REMOTE_NAME}/ {home}/{_BOX_FCT_REMOTE_NAME}/vendor/mngr/"
        ),
        timeout_seconds=300.0,
        label="vendor-sync",
        is_streaming=False,
    )
    if vendor_sync.returncode != 0:
        raise BareMetalProvisioningError(f"vendor/mngr refresh on {address} failed: {vendor_sync.stderr.strip()}")


def _wait_for_container_sentinel(
    *,
    address: str,
    container_port: int,
    private_key_path: Path,
    timeout_seconds: float,
) -> bool:
    """Wait (in the container's shell) for the FCT initial-chat sentinel; return True once present.

    Returns False on timeout (the bootstrap may not have created a chat agent --
    e.g. inference creds absent -- in which case there is nothing to tear down).
    """
    result = _run_remote(
        address=address,
        ssh_user="root",
        private_key_path=private_key_path,
        port=container_port,
        command=build_wait_for_sentinel_container_command(int(timeout_seconds)),
        timeout_seconds=timeout_seconds + 60.0,
        label="sentinel-wait",
        is_streaming=False,
    )
    return result.returncode == 0


def _bake_into_outcomes(
    *,
    capacity: BareMetalServerCapacity,
    overcommit_ratio: float,
    private_key_path: Path,
    pool_public_key: str,
    database_url: str,
    outcomes: list[dict[str, Any]],
    outcomes_lock: "threading.Lock",
) -> None:
    """Thread target: bake one slice and append its outcome under the lock."""
    outcome = _bake_and_record_one_slice(
        capacity=capacity,
        overcommit_ratio=overcommit_ratio,
        private_key_path=private_key_path,
        pool_public_key=pool_public_key,
        database_url=database_url,
    )
    with outcomes_lock:
        outcomes.append(outcome)


def _bake_and_record_one_slice(
    *,
    capacity: BareMetalServerCapacity,
    overcommit_ratio: float,
    private_key_path: Path,
    pool_public_key: str,
    database_url: str,
) -> dict[str, Any]:
    """Bake one slice on its box and insert its pool_hosts row. Returns an outcome dict (never raises)."""
    server = capacity.server
    address = server.public_address
    ssh_user = server.lima_service_user or "root"
    host_name = f"slice-{uuid4().hex}"
    attributes = plan_next_slice_attributes(capacity, overcommit_ratio)
    attributes_json = json.dumps(attributes)
    try:
        if not address:
            raise BareMetalProvisioningError(f"server {server.id} has no public_address; cannot bake")
        home = _remote_home(ssh_user)
        bake_command = build_slice_bake_remote_command(
            fct_dir=f"{home}/{_BOX_FCT_REMOTE_NAME}",
            mngr_bin=f"{home}/{_BOX_MNGR_REMOTE_NAME}/.venv/bin/mngr",
            host_name=host_name,
            attributes_json=attributes_json,
            box_public_address=address,
            pool_public_key=pool_public_key,
        )
        logger.info("Baking slice {} on {} ({})", host_name, server.id, address)
        bake_result = _run_remote(
            address=address,
            ssh_user=ssh_user,
            private_key_path=private_key_path,
            port=22,
            command=bake_command,
            timeout_seconds=_SLICE_BAKE_TIMEOUT_SECONDS,
            label=f"bake:{host_name}",
            is_streaming=True,
        )
        if bake_result.returncode != 0:
            raise BareMetalProvisioningError(
                f"bake of {host_name} on {address} failed (exit {bake_result.returncode}): {bake_result.stderr.strip()}"
            )
        created = parse_create_json_from_output(bake_result.stdout)
        host_id = str(created["host_id"])
        agent_id = str(created["agent_id"])
        container_ssh_port = int(created["ssh_port"])
        vm_ssh_port = int(created["outer_ssh_port"])

        # Tear down the bootstrap-created chat agent + sentinel so the user's
        # first lease re-creates the chat agent under their own workspace name.
        is_sentinel_present = _wait_for_container_sentinel(
            address=address,
            container_port=container_ssh_port,
            private_key_path=private_key_path,
            timeout_seconds=_SENTINEL_POLL_TIMEOUT_SECONDS,
        )
        if is_sentinel_present:
            teardown = _run_remote(
                address=address,
                ssh_user="root",
                private_key_path=private_key_path,
                port=container_ssh_port,
                command=build_chat_teardown_container_command(host_name),
                timeout_seconds=180.0,
                label=f"teardown:{host_name}",
                is_streaming=True,
            )
            if teardown.returncode != 0:
                raise BareMetalProvisioningError(
                    f"chat-agent teardown for {host_name} failed (exit {teardown.returncode}): "
                    f"{teardown.stderr.strip()}"
                )
        else:
            logger.warning("No initial-chat sentinel appeared for {}; skipping chat teardown", host_name)

        # Insert the available slice pool_hosts row (laptop-side).
        host_id_obj = HostId(host_id)
        values = build_slice_pool_host_insert_values(
            row_id=str(uuid4()),
            box_public_address=address,
            agent_id=agent_id,
            host_id=host_id,
            host_name=host_name,
            vm_ssh_host_port=vm_ssh_port,
            container_ssh_host_port=container_ssh_port,
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
        logger.info(
            "Slice {} ready on {} (host_id={}, ports vm={}/container={})",
            host_name,
            address,
            host_id,
            vm_ssh_port,
            container_ssh_port,
        )
        return {
            "host_name": host_name,
            "server_id": str(server.id),
            "host_id": host_id,
            "agent_id": agent_id,
            "vm_ssh_port": vm_ssh_port,
            "container_ssh_port": container_ssh_port,
            "attributes": attributes,
            "status": "succeeded",
        }
    except (BareMetalProvisioningError, psycopg2.Error, KeyError, ValueError, OSError) as exc:
        logger.warning("Slice bake {} failed: {}", host_name, exc)
        return {"host_name": host_name, "server_id": str(server.id), "status": "failed", "error": str(exc)}


@server.command(name="allocate-slice")
@click.option("--count", type=int, default=1, help="Number of slices to bake (placed across ready servers).")
@click.option("--overcommit-ratio", type=float, default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO)
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
    help="mngr monorepo root to sync onto the box (default: this checkout).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Report placement + slice attributes; do not bake.")
@click.option("--database-url", default=None)
def allocate_slice(
    count: int,
    overcommit_ratio: float,
    workspace_dir: str | None,
    mngr_source: str | None,
    dry_run: bool,
    database_url: str | None,
) -> None:
    """Bake one or more slices onto the ready bare-metal fleet and insert their pool rows.

    Picks the ready server(s) with the most free slots, syncs this branch's mngr +
    the FCT workspace onto each chosen box once, then bakes the slices in parallel:
    each carves a lima VM, bakes the shared vps_docker container + FCT workspace,
    authorizes the pool key, tears down the bootstrap chat agent, and inserts an
    ``available`` slice ``pool_hosts`` row. ``--dry-run`` only reports placement.
    """
    if count <= 0:
        raise click.UsageError("--count must be positive")
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    placements = plan_slice_placements(capacities, count)

    if dry_run:
        emit_json(
            {
                "dry_run": True,
                "placements": [
                    {
                        "server_id": str(capacity.server.id),
                        "public_address": capacity.server.public_address,
                        "region": capacity.server.region,
                        "attributes": plan_next_slice_attributes(capacity, overcommit_ratio),
                    }
                    for capacity in placements
                ],
            }
        )
        return

    # Resolve the source trees (default to this checkout for mngr, and the
    # conventional FCT checkout for the workspace).
    repo_root = Path(__file__).resolve().parents[5]
    resolved_mngr_source = Path(mngr_source) if mngr_source else repo_root
    resolved_workspace_dir = (
        Path(workspace_dir) if workspace_dir else Path.home() / "project" / "forever-claude-template"
    )
    if not resolved_workspace_dir.is_dir():
        raise click.UsageError(
            f"FCT workspace not found at {resolved_workspace_dir}; pass --workspace-dir explicitly."
        )

    with _pool_private_key_path() as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        # Sync each chosen box once (serial) before the parallel bakes, so
        # concurrent bakes don't race on the rsync / uv sync.
        unique_servers = {capacity.server.id: capacity.server for capacity in placements}
        for server in unique_servers.values():
            if not server.public_address:
                raise click.UsageError(f"server {server.id} has no public_address; cannot sync")
            _sync_repos_to_box(
                mngr_source=resolved_mngr_source,
                workspace_dir=resolved_workspace_dir,
                address=server.public_address,
                ssh_user=server.lima_service_user or "root",
                private_key_path=private_key_path,
            )

        # Bake all slices in parallel (one thread each); the per-slice port probe
        # keeps concurrent bakes on the same box from colliding.
        outcomes: list[dict[str, Any]] = []
        outcomes_lock = threading.Lock()
        threads = [
            ObservableThread(
                target=_bake_into_outcomes,
                kwargs=dict(
                    capacity=capacity,
                    overcommit_ratio=overcommit_ratio,
                    private_key_path=private_key_path,
                    pool_public_key=pool_public_key,
                    database_url=resolved_database_url,
                    outcomes=outcomes,
                    outcomes_lock=outcomes_lock,
                ),
                name=f"bake-{idx}",
            )
            for idx, capacity in enumerate(placements)
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
