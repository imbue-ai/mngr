from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyTarget
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity

# Wire / DB values for pool_hosts.status. Rows are inserted 'available', flipped to
# 'leased' by the connector's /hosts/lease, and to 'removing' (the durable, retryable
# in-progress teardown marker) by both the connector's release path and the admin
# destroy's atomic claim. 'released' is a legacy value nothing writes anymore (release
# deletes the row); it stays claimable so historical rows can still be destroyed.
POOL_HOST_STATUS_AVAILABLE: Final[str] = "available"
POOL_HOST_STATUS_LEASED: Final[str] = "leased"
POOL_HOST_STATUS_REMOVING: Final[str] = "removing"
POOL_HOST_STATUS_RELEASED: Final[str] = "released"

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

# A slice is an ordinary pool_hosts row plus the lima fields the connector needs
# to tear it down. ssh_port / container_ssh_port are the box-forwarded ports (not
# the default 22 / 2222), so they are params.
_INSERT_SLICE_POOL_HOST_SQL: Final[str] = (
    "INSERT INTO pool_hosts "
    "(id, vps_address, vps_instance_id, agent_id, host_id, host_name, ssh_port, ssh_user, "
    "container_ssh_port, status, attributes, region, bare_metal_server_id, "
    "lima_instance_name, lima_disk_name, outer_host_public_key, container_host_public_key, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'root', %s, 'available', %s::jsonb, %s, "
    "%s, %s, %s, %s, %s, NOW())"
)

_SELECT_SERVERS_SQL: Final[str] = (
    "SELECT id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, lima_service_user, status, "
    "created_at, updated_at, box_host_public_key FROM bare_metal_servers ORDER BY created_at ASC"
)

# Count the baked slices currently on a server. Every row -- including 'removing'
# ones, whose VM teardown may still be in flight -- occupies its box slot until the
# VM is destroyed and the row deleted, so a server's free slots = slot_count - this
# count stays truthful while destroys run.
_COUNT_SLICES_SQL: Final[str] = "SELECT COUNT(*) FROM pool_hosts WHERE bare_metal_server_id = %s"

# Every slice's lima instance name on a server (any status). Used to reconcile the
# box's running VMs against the DB and reap orphans (VMs with no row).
_SELECT_SLICE_INSTANCE_NAMES_SQL: Final[str] = (
    "SELECT lima_instance_name FROM pool_hosts WHERE bare_metal_server_id = %s AND lima_instance_name IS NOT NULL"
)

# Sibling of the instance-name query, for reconciling the box's lima data disks
# against the DB and reaping orphan disks (disks with no row).
_SELECT_SLICE_DISK_NAMES_SQL: Final[str] = (
    "SELECT lima_disk_name FROM pool_hosts WHERE bare_metal_server_id = %s AND lima_disk_name IS NOT NULL"
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
    # Baked sshd host public keys (deterministic, from `mngr create --format json`):
    # the VM-root key and the inner container key, persisted so leasing pins them.
    outer_host_public_key: str,
    container_host_public_key: str,
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
        outer_host_public_key,
        container_host_public_key,
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
        box_host_public_key=row[18],
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


def fetch_server_by_id(conn: Any, server_id: BareMetalServerDbId) -> BareMetalServer | None:
    """Return a single bare_metal_servers row by id, or None if it does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SERVERS_SQL.replace("ORDER BY created_at ASC", "WHERE id = %s"), (str(server_id),))
        row = cur.fetchone()
    return _server_from_row(row) if row else None


def count_slices_on_server(conn: Any, server_id: BareMetalServerDbId) -> int:
    """Count the baked (non-removing) slices currently on a server."""
    with conn.cursor() as cur:
        cur.execute(_COUNT_SLICES_SQL, (str(server_id),))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def fetch_server_capacities(conn: Any) -> list[BareMetalServerCapacity]:
    """Return every server paired with its slice-slot accounting (used / free)."""
    return [compute_capacity(server, count_slices_on_server(conn, server.id)) for server in fetch_servers(conn)]


def fetch_slice_instance_names_for_server(conn: Any, server_id: BareMetalServerDbId) -> set[str]:
    """Return the lima_instance_name of every slice pool_hosts row for ``server_id`` (any status)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SLICE_INSTANCE_NAMES_SQL, (str(server_id),))
        return {row[0] for row in cur.fetchall() if row[0]}


def fetch_slice_disk_names_for_server(conn: Any, server_id: BareMetalServerDbId) -> set[str]:
    """Return the lima_disk_name of every slice pool_hosts row for ``server_id`` (any status)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SLICE_DISK_NAMES_SQL, (str(server_id),))
        return {row[0] for row in cur.fetchall() if row[0]}


def insert_slice_pool_host(conn: Any, values: tuple[Any, ...]) -> None:
    """Insert a slice pool_hosts row (values from build_slice_pool_host_insert_values)."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_SLICE_POOL_HOST_SQL, values)
    conn.commit()


# Unleased slice row ids -- the pool backlog that an env destroy must tear down so it
# does not leak VMs once the env's DB is gone. Leased slices are deliberately
# excluded: they are torn down via their agent's release path (`mngr destroy` ->
# connector release), and tearing their VM down here would race that path.
# ``removing`` rows ARE included: a row stranded mid-teardown (a crashed release, or
# a prior destroy whose box was unreachable) would otherwise never be cleaned up --
# both the VM destroy and the row delete are idempotent, so re-tearing one down is
# harmless even against an in-flight release.
_SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL: Final[str] = (
    "SELECT p.id "
    "FROM pool_hosts p JOIN bare_metal_servers s ON p.bare_metal_server_id = s.id "
    "WHERE p.status != 'leased' "
    "AND p.lima_instance_name IS NOT NULL AND s.public_address IS NOT NULL"
)


def fetch_unleased_slice_teardown_row_ids(conn: Any) -> list[str]:
    """Return the row id of every unleased slice whose box is still reachable in the DB."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL)
        return [str(row[0]) for row in cur.fetchall()]


# Atomically claim a row for teardown by flipping it to 'removing', but only from a
# caller-approved status set -- the WHERE makes the claim and the eligibility check a
# single statement, so it cannot race the connector's lease (which only ever selects
# 'available' rows, under FOR UPDATE): either the lease commits first and the claim
# matches nothing, or the claim commits first and the row is invisible to leasing.
_CLAIM_POOL_HOST_FOR_REMOVAL_SQL: Final[str] = (
    "UPDATE pool_hosts SET status = 'removing' WHERE id = %s AND status = ANY(%s)"
)

_SELECT_POOL_HOST_STATUS_SQL: Final[str] = "SELECT status FROM pool_hosts WHERE id = %s"

# The claimed row's teardown coordinates. LEFT JOIN so a row whose box record was
# deleted still comes back (with null box columns) and can be reported precisely.
_SELECT_POOL_HOST_DESTROY_TARGET_SQL: Final[str] = (
    "SELECT p.id, p.lima_instance_name, s.public_address, s.lima_service_user, s.box_host_public_key "
    "FROM pool_hosts p LEFT JOIN bare_metal_servers s ON p.bare_metal_server_id = s.id "
    "WHERE p.id = %s"
)


@pure
def destroy_eligible_pool_host_statuses(is_leased_destroy_allowed: bool) -> tuple[str, ...]:
    """The statuses an admin destroy may atomically claim.

    'removing' is always claimable so a destroy that failed mid-teardown can be
    retried by re-running with the same id; 'leased' requires the explicit
    ``--force`` opt-in (it tears down a user's live workspace).
    """
    base = (POOL_HOST_STATUS_AVAILABLE, POOL_HOST_STATUS_RELEASED, POOL_HOST_STATUS_REMOVING)
    if is_leased_destroy_allowed:
        return base + (POOL_HOST_STATUS_LEASED,)
    return base


def claim_pool_host_for_removal(conn: Any, row_id: str, eligible_statuses: Sequence[str]) -> bool:
    """Atomically flip a row to 'removing' (committing immediately); True if this call claimed it.

    False means the row is gone or in a non-eligible status (e.g. it was leased
    between the operator listing it and the destroy running).
    """
    with conn.cursor() as cur:
        cur.execute(_CLAIM_POOL_HOST_FOR_REMOVAL_SQL, (row_id, list(eligible_statuses)))
        is_claimed = cur.rowcount == 1
    conn.commit()
    return is_claimed


def fetch_pool_host_status(conn: Any, row_id: str) -> str | None:
    """Return a pool_hosts row's status, or None if the row does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_POOL_HOST_STATUS_SQL, (row_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def fetch_pool_host_destroy_target(conn: Any, row_id: str) -> PoolHostDestroyTarget | None:
    """Return a row's teardown coordinates (box columns None when the box record is gone)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_POOL_HOST_DESTROY_TARGET_SQL, (row_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return PoolHostDestroyTarget(
        pool_host_row_id=str(row[0]),
        lima_instance_name=str(row[1]) if row[1] else None,
        box_public_address=str(row[2]) if row[2] else None,
        lima_service_user=str(row[3]) if row[3] else None,
        box_host_public_key=row[4],
    )


def delete_pool_host_row(conn: Any, row_id: str) -> None:
    """Delete a single pool_hosts row by id (committing immediately)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (row_id,))
    conn.commit()
