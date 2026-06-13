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

import json
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

import click
import psycopg2
from loguru import logger

from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.bare_metal import DEFAULT_SLICE_FALLBACK_CPU_THREADS
from imbue.mngr_imbue_cloud.bare_metal import SLICE_ADVERTISED_RAM_GB
from imbue.mngr_imbue_cloud.bare_metal import choose_server_for_new_slice
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.bare_metal_db import fetch_server_capacities
from imbue.mngr_imbue_cloud.bare_metal_db import insert_bare_metal_server
from imbue.mngr_imbue_cloud.bare_metal_db import update_server
from imbue.mngr_imbue_cloud.cli.admin import resolve_pool_database_url
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


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


@server.command(name="allocate-slice")
@click.option("--overcommit-ratio", type=float, default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO)
@click.option("--database-url", default=None)
def allocate_slice(overcommit_ratio: float, database_url: str | None) -> None:
    """Pick the ready server with the most free slots and report the planned slice attributes.

    The actual bake (carving the lima VM via SliceVpsDockerProvider and inserting
    the slice pool_hosts row) runs on the box during the operator flow; this
    command resolves placement + the slice's lease attributes from the live DB.
    """
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    chosen = choose_server_for_new_slice(capacities)
    attributes = plan_next_slice_attributes(chosen, overcommit_ratio)
    logger.info(
        "Allocate next slice on server {} ({}); {} free slots; slice attributes: {}",
        chosen.server.id,
        chosen.server.public_address,
        chosen.free_slots,
        json.dumps(attributes),
    )


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
