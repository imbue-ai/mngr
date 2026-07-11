from datetime import datetime
from datetime import timezone

from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _CLAIM_POOL_HOST_FOR_REMOVAL_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _COUNT_SLICES_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _INSERT_BARE_METAL_SERVER_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _INSERT_SLICE_POOL_HOST_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _server_from_row
from imbue.mngr_imbue_cloud.slices.bare_metal_db import build_bare_metal_server_insert_values
from imbue.mngr_imbue_cloud.slices.bare_metal_db import build_slice_pool_host_insert_values
from imbue.mngr_imbue_cloud.slices.bare_metal_db import claim_pool_host_for_removal
from imbue.mngr_imbue_cloud.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_pool_host_destroy_target
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_unleased_slice_teardown_row_ids


def _ready_server() -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        ovh_order_id="8144904",
        ovh_service_name="ns1012536.ip-15-204-140.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_cores=8,
        cpu_threads=16,
        ram_gb=64,
        disk_gb=477,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        slot_count=8,
        raid_level="RAID1",
        lima_service_user="limahost",
        box_host_public_key="ssh-ed25519 AAAAbox",
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
    )


def test_server_insert_placeholder_count_matches_builder() -> None:
    # Every %s placeholder must line up with exactly one builder value.
    values = build_bare_metal_server_insert_values(_ready_server())
    assert _INSERT_BARE_METAL_SERVER_SQL.count("%s") == len(values)


def test_server_insert_values_are_in_column_order() -> None:
    values = build_bare_metal_server_insert_values(_ready_server())
    assert values == (
        "11111111-1111-1111-1111-111111111111",
        "8144904",
        "ns1012536.ip-15-204-140.us",
        "24rise02-v1-us",
        "vin",
        "15.204.140.221",
        8,
        16,
        64,
        477,
        8,
        1.5,
        8,
        "RAID1",
        "limahost",
        "ready",
    )


def test_slice_pool_host_insert_placeholder_count_matches_builder() -> None:
    values = build_slice_pool_host_insert_values(
        row_id="row-1",
        box_public_address="15.204.140.221",
        agent_id="agent-1",
        host_id="host-1",
        host_name="ws-1",
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
        attributes_json='{"memory_gb": 8}',
        region="vin",
        bare_metal_server_id="srv-1",
        lima_instance_name="mngr-slice-abc",
        lima_disk_name="mngr-slice-abc-data",
        outer_host_public_key="ssh-ed25519 AAAAouter",
        container_host_public_key="ssh-ed25519 AAAAcontainer",
    )
    assert _INSERT_SLICE_POOL_HOST_SQL.count("%s") == len(values)


def test_slice_pool_host_insert_uses_lima_instance_as_vps_instance_id() -> None:
    values = build_slice_pool_host_insert_values(
        row_id="row-1",
        box_public_address="15.204.140.221",
        agent_id="agent-1",
        host_id="host-1",
        host_name="ws-1",
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
        attributes_json="{}",
        region="vin",
        bare_metal_server_id="srv-1",
        lima_instance_name="mngr-slice-abc",
        lima_disk_name="mngr-slice-abc-data",
        outer_host_public_key="ssh-ed25519 AAAAouter",
        container_host_public_key="ssh-ed25519 AAAAcontainer",
    )
    # vps_address is the box; vps_instance_id is the (non-null) lima instance;
    # the two forwarded ports are carried verbatim.
    assert values[1] == "15.204.140.221"
    assert values[2] == "mngr-slice-abc"
    assert values[6] == 22001
    assert values[7] == 22002


def test_server_from_row_round_trips() -> None:
    server = _ready_server()
    # _server_from_row reads the SELECT column order: the insert values, then
    # created_at / updated_at, then box_host_public_key.
    row = build_bare_metal_server_insert_values(server) + (
        server.created_at,
        server.updated_at,
        server.box_host_public_key,
    )
    reconstructed = _server_from_row(row)
    assert reconstructed.id == server.id
    assert reconstructed.ovh_service_name == server.ovh_service_name
    assert reconstructed.slot_count == 8
    assert str(reconstructed.status) == "ready"
    assert reconstructed.ram_gb == 64
    assert reconstructed.box_host_public_key == "ssh-ed25519 AAAAbox"


class _FakeCursor:
    """Minimal cursor that returns scripted rows and records executed SQL (no real DB)."""

    def __init__(self, rows: list[tuple], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.executed_sql: str | None = None
        self.executed_params: tuple | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed_sql = sql
        self.executed_params = params

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Minimal connection yielding a scripted cursor (no real DB)."""

    def __init__(self, rows: list[tuple], rowcount: int) -> None:
        self._cursor = _FakeCursor(rows, rowcount)
        self.commit_count = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_count += 1


def test_fetch_unleased_slice_teardown_row_ids_maps_rows_to_strings() -> None:
    fake_conn = _FakeConn([("row-1",), ("row-2",)], rowcount=0)
    eligible = destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    row_ids = fetch_unleased_slice_teardown_row_ids(fake_conn, eligible)
    assert row_ids == ["row-1", "row-2"]
    # The status filter is the SAME claimable set the destroy uses, passed as a bind
    # parameter -- so a selected row is always claimable and the predicates can't drift.
    assert fake_conn._cursor.executed_params == (list(eligible),)


def test_unleased_teardown_query_filters_by_the_claimable_status_set() -> None:
    # Leased slices are torn down by their agent's release path and must be excluded --
    # which falls out of the claimable set (it omits 'leased' without --force). 'removing'
    # rows ARE included: a row stranded mid-teardown (crashed release or a destroy whose
    # box was unreachable) must not leak when the env is destroyed.
    assert "p.status = ANY(%s)" in _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL
    assert "leased" not in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    assert "removing" in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    # The inner JOIN on bare_metal_server_id already restricts to slice rows.
    assert "JOIN bare_metal_servers" in _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL


def test_destroy_eligible_statuses_require_force_for_leased() -> None:
    # The default claim set covers available rows, stale 'removing' rows (a retry of a
    # prior failed teardown), and the legacy 'released' value; only --force adds leased.
    assert destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False) == (
        "available",
        "released",
        "removing",
    )
    assert "leased" in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=True)


def test_claim_pool_host_for_removal_is_a_single_conditional_update() -> None:
    # The claim must flip the status and check eligibility in ONE statement -- that
    # atomicity (vs the connector's lease, which only selects 'available' rows) is
    # what closes the destroy-vs-lease race.
    assert _CLAIM_POOL_HOST_FOR_REMOVAL_SQL.startswith("UPDATE pool_hosts SET status = 'removing'")
    assert "status = ANY(%s)" in _CLAIM_POOL_HOST_FOR_REMOVAL_SQL


def test_claim_pool_host_for_removal_reports_whether_the_row_was_claimed() -> None:
    claimed_conn = _FakeConn([], rowcount=1)
    assert claim_pool_host_for_removal(claimed_conn, "row-1", ("available",)) is True
    assert claimed_conn.commit_count == 1
    missed_conn = _FakeConn([], rowcount=0)
    assert claim_pool_host_for_removal(missed_conn, "row-1", ("available",)) is False
    # The miss is still committed so the (no-op) transaction does not linger.
    assert missed_conn.commit_count == 1


def test_fetch_pool_host_destroy_target_maps_null_box_columns() -> None:
    # A row whose box record was deleted still comes back (LEFT JOIN), with null box
    # columns, so the caller can report it precisely instead of erroring on lookup.
    target = fetch_pool_host_destroy_target(_FakeConn([("mngr-slice-dev-a", None, None, None)], rowcount=0), "row-1")
    assert target is not None
    assert target.lima_instance_name == "mngr-slice-dev-a"
    assert target.box_public_address is None
    assert target.lima_service_user is None
    assert fetch_pool_host_destroy_target(_FakeConn([], rowcount=0), "row-gone") is None


def test_count_slices_counts_removing_rows_as_occupied() -> None:
    # A 'removing' row's VM may still be tearing down; it holds its box slot until the
    # VM is destroyed and the row deleted, so slot accounting must not exclude it.
    assert "status" not in _COUNT_SLICES_SQL
