import json

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.machines import _find_machine_by_ref
from imbue.mngr_imbue_cloud.cli.machines import _machine_display_payload
from imbue.mngr_imbue_cloud.cli.machines import machines
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

_HOST_ID = "host-" + "a" * 32


def _make_workspace(
    host_id: str = _HOST_ID,
    host_name: str = "my-workspace",
    memory_units: int | None = 8,
    target_memory_units: int | None = None,
    disk_gb: int | None = 28,
    target_disk_gb: int | None = None,
) -> WorkspaceInfo:
    return WorkspaceInfo(
        host_db_id=LeaseDbId("11111111-2222-3333-4444-555555555555"),
        status=WorkspaceStatus.RUNNING,
        agent_id="agent-" + "b" * 32,
        host_id=host_id,
        host_name=host_name,
        memory_units=memory_units,
        target_memory_units=target_memory_units,
        disk_gb=disk_gb,
        target_disk_gb=target_disk_gb,
    )


def test_machines_group_lists_show_and_resize() -> None:
    result = CliRunner().invoke(machines, ["--help"])
    assert result.exit_code == 0
    assert "show" in result.output
    assert "resize" in result.output


def test_resize_help_documents_the_record_then_restart_contract() -> None:
    result = CliRunner().invoke(machines, ["resize", "--help"])
    assert result.exit_code == 0
    assert "--units" in result.output
    assert "--disk-gb" in result.output
    assert "next restart" in result.output


def test_resize_rejects_disallowed_unit_sizes_before_any_network_call() -> None:
    for bad_units in ("0", "-8", "4", "12", "136"):
        result = CliRunner().invoke(machines, ["resize", "m", "--units", bad_units])
        assert result.exit_code != 0, bad_units
        payload = json.loads(result.output)
        assert payload["error_class"] == "UsageError"
        assert "multiple of 8" in payload["error"]


def test_resize_requires_at_least_one_target() -> None:
    result = CliRunner().invoke(machines, ["resize", "m"])
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["error_class"] == "UsageError"


def test_find_machine_by_ref_matches_host_id_db_id_and_name() -> None:
    entry = _make_workspace()
    workspaces = [entry]
    assert _find_machine_by_ref(workspaces, _HOST_ID) is entry
    assert _find_machine_by_ref(workspaces, "11111111-2222-3333-4444-555555555555") is entry
    assert _find_machine_by_ref(workspaces, "my-workspace") is entry
    assert _find_machine_by_ref(workspaces, "host-" + "f" * 32) is None


def test_machine_display_payload_flags_a_pending_restart() -> None:
    pending = _machine_display_payload(_make_workspace(target_memory_units=16))
    assert pending["is_restart_needed_to_apply"] is True
    assert pending["memory_units"] == 8
    assert pending["target_memory_units"] == 16
    settled = _machine_display_payload(_make_workspace())
    assert settled["is_restart_needed_to_apply"] is False
    assert settled["status"] == "running"
