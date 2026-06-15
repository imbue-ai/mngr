from datetime import datetime
from datetime import timezone

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.server import _format_capacity_table
from imbue.mngr_imbue_cloud.cli.server import build_registered_server
from imbue.mngr_imbue_cloud.cli.server import compute_server_slice_sizing
from imbue.mngr_imbue_cloud.cli.server import server
from imbue.mngr_imbue_cloud.cli.server import slice_advertised_attributes
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity


def _server(
    slot_count: int,
    cpu_threads: int,
    *,
    memory_per_slice_gb: int = 8,
    cpu_overcommit_ratio: float = 1.5,
    disk_gb: int = 477,
) -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_threads=cpu_threads,
        ram_gb=slot_count * memory_per_slice_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit_ratio,
        slot_count=slot_count,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
    )


def test_build_registered_server_derives_slot_count_from_memory_per_slice() -> None:
    built = build_registered_server(
        ovh_service_name="ns1.ovh.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="1.2.3.4",
        ram_gb=64,
        cpu_cores=8,
        cpu_threads=16,
        disk_gb=477,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        raid_level="RAID1",
        lima_service_user="limahost",
        ovh_order_id="8144904",
        status=SERVER_STATUS_READY,
    )
    assert built.slot_count == 8
    assert built.disk_gb == 477
    assert built.ovh_service_name == "ns1.ovh.us"
    assert str(built.status) == "ready"


def test_compute_server_slice_sizing_uses_server_inputs_and_specs() -> None:
    sizing = compute_server_slice_sizing(_server(slot_count=8, cpu_threads=16))
    # 16 threads * 1.5 / 8 slots = 3 vCPU per slice.
    assert sizing["vcpus"] == 3
    assert sizing["advertised_memory_gb"] == 8
    assert sizing["memory_mib"] == 8 * 1024 - 512
    # Per-slice disk budget (477 - 20 reserve) // 8 slots, minus the fixed boot disk.
    assert sizing["disk_gib"] == (477 - 20) // 8 - SLICE_BOOT_DISK_GIB
    assert slice_advertised_attributes(sizing) == {"memory_gb": 8, "cpus": 3}


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
    # The server group holds only the fleet-lifecycle verbs; slice baking moved to
    # ``admin pool create --backend slice``.
    for command in ("prep", "list", "register", "set-status"):
        assert command in result.output
    assert "allocate-slice" not in result.output
