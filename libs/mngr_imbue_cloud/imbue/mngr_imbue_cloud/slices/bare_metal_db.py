from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity

# Admin tooling writes bare_metal_servers + slice pool_hosts rows directly to the
# connector's host_pool Neon DB (laptop-side), mirroring how `admin pool create`
# writes VPS pool_hosts rows. The connector only reads these (plus its release
# writes). Keep the column lists in sync with migrations 008 / 009.

_INSERT_BARE_METAL_SERVER_SQL: Final[str] = (
    "INSERT INTO bare_metal_servers "
    "(id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, lima_service_user, status, created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())"
)

# A slice is an ordinary pool_hosts row with backend_kind='slice' + the lima
# fields the connector needs to tear it down. ssh_port / container_ssh_port are
# the box-forwarded ports (not the OVH default 22 / 2222), so they are params.
_INSERT_SLICE_POOL_HOST_SQL: Final[str] = (
    "INSERT INTO pool_hosts "
    "(id, vps_address, vps_instance_id, agent_id, host_id, host_name, ssh_port, ssh_user, "
    "container_ssh_port, status, attributes, region, backend_kind, bare_metal_server_id, "
    "lima_instance_name, lima_disk_name, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'root', %s, 'available', %s::jsonb, %s, "
    "'slice', %s, %s, %s, NOW())"
)

_SELECT_SERVERS_SQL: Final[str] = (
    "SELECT id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, lima_service_user, status, "
    "created_at, updated_at FROM bare_metal_servers ORDER BY created_at ASC"
)

# Count the baked slices currently on a server (any non-removed pool_hosts row);
# a server's free slots = slot_count - this count.
_COUNT_SLICES_SQL: Final[str] = (
    "SELECT COUNT(*) FROM pool_hosts WHERE bare_metal_server_id = %s AND status != 'removing'"
)


@pure
def build_bare_metal_server_insert_values(server: BareMetalServer) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_INSERT_BARE_METAL_SERVER_SQL` from a server."""
    return (
        str(server.id),
        server.ovh_order_id,
        server.ovh_service_name,
        server.plan_code,
        server.region,
        server.public_address,
        server.cpu_cores,
        server.cpu_threads,
        server.ram_gb,
        server.disk_gb,
        server.memory_per_slice_gb,
        server.cpu_overcommit_ratio,
        server.slot_count,
        server.raid_level,
        server.lima_service_user,
        str(server.status),
    )


@pure
def build_slice_pool_host_insert_values(
    *,
    row_id: str,
    box_public_address: str,
    agent_id: str,
    host_id: str,
    host_name: str,
    vm_ssh_host_port: int,
    container_ssh_host_port: int,
    attributes_json: str,
    region: str,
    bare_metal_server_id: str,
    lima_instance_name: str,
    lima_disk_name: str,
) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_INSERT_SLICE_POOL_HOST_SQL`.

    ``vps_address`` is the box's public address and ``vps_instance_id`` is set to
    the lima instance name (the column is NOT NULL and slice teardown keys on the
    lima fields, not on an OVH service name). ``ssh_port`` / ``container_ssh_port``
    are the box-forwarded ports for the VM's root sshd and the inner container sshd.
    """
    return (
        row_id,
        box_public_address,
        # vps_instance_id: non-null placeholder; slices are torn down via the lima fields.
        lima_instance_name,
        agent_id,
        host_id,
        host_name,
        vm_ssh_host_port,
        container_ssh_host_port,
        attributes_json,
        region,
        bare_metal_server_id,
        lima_instance_name,
        lima_disk_name,
    )


@pure
def _as_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.now(timezone.utc)


@pure
def _server_from_row(row: tuple[Any, ...]) -> BareMetalServer:
    return BareMetalServer(
        id=BareMetalServerDbId(str(row[0])),
        ovh_order_id=row[1],
        ovh_service_name=row[2],
        plan_code=str(row[3]),
        region=str(row[4]),
        public_address=row[5],
        cpu_cores=row[6],
        cpu_threads=row[7],
        ram_gb=row[8],
        disk_gb=row[9],
        memory_per_slice_gb=row[10],
        cpu_overcommit_ratio=float(row[11]) if row[11] is not None else None,
        slot_count=int(row[12]) if row[12] is not None else 0,
        raid_level=row[13],
        lima_service_user=row[14],
        status=BareMetalServerStatus(str(row[15])),
        created_at=_as_datetime(row[16]),
        updated_at=_as_datetime(row[17]),
    )


def insert_bare_metal_server(conn: Any, server: BareMetalServer) -> None:
    """Insert a new bare_metal_servers row."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_BARE_METAL_SERVER_SQL, build_bare_metal_server_insert_values(server))
    conn.commit()


def update_server(conn: Any, server_id: BareMetalServerDbId, **fields: Any) -> None:
    """Update the named columns of a bare_metal_servers row (always bumps updated_at)."""
    if not fields:
        return
    assignments = ", ".join(f"{column} = %s" for column in fields)
    params = [*fields.values(), str(server_id)]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE bare_metal_servers SET {assignments}, updated_at = NOW() WHERE id = %s",
            tuple(params),
        )
    conn.commit()


def fetch_servers(conn: Any) -> list[BareMetalServer]:
    """Return all bare_metal_servers rows, oldest first."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SERVERS_SQL)
        rows = cur.fetchall()
    return [_server_from_row(row) for row in rows]


def count_slices_on_server(conn: Any, server_id: BareMetalServerDbId) -> int:
    """Count the baked (non-removing) slices currently on a server."""
    with conn.cursor() as cur:
        cur.execute(_COUNT_SLICES_SQL, (str(server_id),))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def fetch_server_capacities(conn: Any) -> list[BareMetalServerCapacity]:
    """Return every server paired with its slice-slot accounting (used / free)."""
    return [compute_capacity(server, count_slices_on_server(conn, server.id)) for server in fetch_servers(conn)]


def insert_slice_pool_host(conn: Any, values: tuple[Any, ...]) -> None:
    """Insert a slice pool_hosts row (values from build_slice_pool_host_insert_values)."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_SLICE_POOL_HOST_SQL, values)
    conn.commit()
