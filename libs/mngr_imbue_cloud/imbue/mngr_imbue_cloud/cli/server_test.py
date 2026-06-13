from datetime import datetime
from datetime import timezone

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.cli.server import _format_capacity_table
from imbue.mngr_imbue_cloud.cli.server import build_registered_server
from imbue.mngr_imbue_cloud.cli.server import plan_next_slice_attributes
from imbue.mngr_imbue_cloud.cli.server import server
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


def _server(slot_count: int, cpu_threads: int) -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_threads=cpu_threads,
        ram_gb=slot_count * 8,
        slot_count=slot_count,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
    )


def test_build_registered_server_derives_slot_count_from_ram() -> None:
    built = build_registered_server(
        ovh_service_name="ns1.ovh.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="1.2.3.4",
        ram_gb=64,
        cpu_cores=8,
        cpu_threads=16,
        raid_level="RAID1",
        lima_service_user="limahost",
        ovh_order_id="8144904",
        status=SERVER_STATUS_READY,
    )
    assert built.slot_count == 8
    assert built.ovh_service_name == "ns1.ovh.us"
    assert str(built.status) == "ready"


def test_plan_next_slice_attributes_advertises_8gb_and_overcommit_cpus() -> None:
    capacity = compute_capacity(_server(slot_count=8, cpu_threads=16), used_slots=0)
    attributes = plan_next_slice_attributes(capacity, overcommit_ratio=1.5)
    assert attributes["memory_gb"] == 8
    # 16 threads * 1.5 / 8 slots = 3 vCPU per slice.
    assert attributes["cpus"] == 3


def test_format_capacity_table_shows_per_server_and_fleet_totals() -> None:
    capacities = [
        compute_capacity(_server(slot_count=8, cpu_threads=16), used_slots=3),
        compute_capacity(_server(slot_count=16, cpu_threads=32), used_slots=1),
    ]
    table = _format_capacity_table(capacities)
    assert "3/8" in table
    assert "1/16" in table
    # Fleet line: 24 total slots, 4 used, 20 free.
    assert "4/24 slots used, 20 free" in table


def test_server_group_help_lists_commands() -> None:
    result = CliRunner().invoke(server, ["--help"])
    assert result.exit_code == 0
    for command in ("list", "register", "allocate-slice", "set-status"):
        assert command in result.output
