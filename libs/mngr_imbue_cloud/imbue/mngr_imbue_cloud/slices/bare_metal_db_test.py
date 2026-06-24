from datetime import datetime
from datetime import timezone

from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _INSERT_BARE_METAL_SERVER_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _INSERT_SLICE_POOL_HOST_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _SELECT_UNLEASED_SLICE_TEARDOWN_TARGETS_SQL
from imbue.mngr_imbue_cloud.slices.bare_metal_db import _server_from_row
from imbue.mngr_imbue_cloud.slices.bare_metal_db import build_bare_metal_server_insert_values
from imbue.mngr_imbue_cloud.slices.bare_metal_db import build_slice_pool_host_insert_values
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_unleased_slice_teardown_targets


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
    """Minimal cursor that returns scripted rows for the one SELECT under test."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.executed_sql: str | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed_sql = sql

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    """Minimal connection yielding a scripted cursor (no real DB)."""

    def __init__(self, rows: list[tuple]) -> None:
        self._cursor = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_fetch_unleased_slice_teardown_targets_maps_rows_to_targets() -> None:
    rows = [
        (
            "row-1",
            "mngr-slice-dev-josh-aaa",
            "mngr-slice-dev-josh-aaa-data",
            "15.0.0.1",
            "limahost",
            "ssh-ed25519 AAAAbox1",
        ),
        # A row whose lima_service_user is NULL falls back to root.
        ("row-2", "mngr-slice-dev-josh-bbb", None, "15.0.0.2", None, None),
    ]
    targets = fetch_unleased_slice_teardown_targets(_FakeConn(rows))
    assert [t.pool_host_row_id for t in targets] == ["row-1", "row-2"]
    assert targets[0].lima_disk_name == "mngr-slice-dev-josh-aaa-data"
    assert targets[1].lima_disk_name is None
    assert targets[0].box_public_address == "15.0.0.1"
    assert targets[1].lima_service_user == "root"
    assert targets[0].box_host_public_key == "ssh-ed25519 AAAAbox1"


def test_fetch_unleased_slice_teardown_targets_query_excludes_leased_and_removing() -> None:
    # Leased slices are torn down by their agent's release path; removing rows are
    # already mid-teardown by the connector sweep -- both must be excluded here.
    assert "NOT IN ('leased', 'removing')" in _SELECT_UNLEASED_SLICE_TEARDOWN_TARGETS_SQL
    assert "backend_kind = 'slice'" in _SELECT_UNLEASED_SLICE_TEARDOWN_TARGETS_SQL
