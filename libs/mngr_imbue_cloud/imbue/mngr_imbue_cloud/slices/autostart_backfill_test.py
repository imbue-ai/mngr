from imbue.mngr_imbue_cloud.slices.autostart_backfill import DEFAULT_SERVICES_AGENT_PATH
from imbue.mngr_imbue_cloud.slices.autostart_backfill import SliceAutostartBackfillOutcome
from imbue.mngr_imbue_cloud.slices.autostart_backfill import SliceAutostartBackfillStatus
from imbue.mngr_imbue_cloud.slices.autostart_backfill import backfill_box_autostart
from imbue.mngr_imbue_cloud.slices.autostart_backfill import build_autostart_backfill_report
from imbue.mngr_imbue_cloud.slices.autostart_backfill import build_autostart_installer_script
from imbue.mngr_imbue_cloud.slices.autostart_backfill import extract_services_agent_path
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

_OLD_LAYOUT_PATH = "/mngr/code/scripts/minds_start_services_agent.sh"

_OLD_AUTOSTART_SCRIPT = """#!/bin/sh
for cid in $(docker ps -aq --filter "label=com.imbue.mngr.host-id"); do
    docker start "$cid" >/dev/null 2>&1 || true
    docker exec --workdir / "$cid" bash -lc 'exec /mngr/code/scripts/minds_start_services_agent.sh' || true
done
"""


def test_extract_services_agent_path_reads_the_old_layout() -> None:
    assert extract_services_agent_path(_OLD_AUTOSTART_SCRIPT) == _OLD_LAYOUT_PATH


def test_extract_services_agent_path_reads_the_current_layout() -> None:
    current_script = build_autostart_installer_script(DEFAULT_SERVICES_AGENT_PATH)
    assert extract_services_agent_path(current_script) == DEFAULT_SERVICES_AGENT_PATH


def test_extract_services_agent_path_returns_none_when_absent() -> None:
    assert extract_services_agent_path("#!/bin/sh\necho nothing here\n") is None


def test_build_autostart_installer_script_substitutes_the_per_vm_path() -> None:
    rendered = build_autostart_installer_script(_OLD_LAYOUT_PATH)
    assert _OLD_LAYOUT_PATH in rendered
    assert DEFAULT_SERVICES_AGENT_PATH not in rendered
    # The rest of the installer (units, marker, enablement) is untouched.
    assert "PathExists=/mngr-btrfs/.minds-volume-ready" in rendered
    assert "systemctl enable minds-autostart.path" in rendered


class _ScriptedBoxClient(LimaSliceVpsClient):
    """Slice-box client double that answers from an in-memory per-VM script map."""

    # Keyed by VM name: the existing autostart script text, or None for a VM
    # without one. Populated via model fields so no real SSH happens.
    autostart_script_by_vm: dict[str, str | None] = {}
    is_read_failing: bool = False
    is_install_failing: bool = False
    installed_scripts_by_vm: dict[str, str] = {}

    def list_instance_names(self) -> set[str]:
        return set(self.autostart_script_by_vm.keys()) | {"not-a-slice"}

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        vm_name = next(name for name in self.autostart_script_by_vm if name in remote_command)
        if "cat /usr/local/sbin/minds-outer-autostart.sh" in remote_command:
            if self.is_read_failing:
                # The limactl shell invocation itself failing (e.g. a stopped
                # VM): non-zero despite the in-VM command's trailing `|| true`.
                return 1, "", "limactl shell exploded"
            existing_script = self.autostart_script_by_vm[vm_name]
            # The read command ends with `|| true`, so a missing file is rc 0
            # with empty stdout -- mirror that.
            return 0, existing_script or "", ""
        if "systemctl is-active minds-autostart.path" in remote_command:
            return (1, "inactive\n", "") if self.is_install_failing else (0, "active\n", "")
        if self.is_install_failing:
            return 1, "", "installer exploded"
        self.installed_scripts_by_vm[vm_name] = remote_command
        return 0, "", ""


def _make_client(
    autostart_script_by_vm: dict[str, str | None],
    is_install_failing: bool = False,
    is_read_failing: bool = False,
) -> _ScriptedBoxClient:
    return _ScriptedBoxClient(
        box_address="203.0.113.5",
        box_ssh_user="limahost",
        private_key_path="/dev/null",
        box_host_public_key=None,
        autostart_script_by_vm=autostart_script_by_vm,
        is_read_failing=is_read_failing,
        is_install_failing=is_install_failing,
        installed_scripts_by_vm={},
    )


def test_backfill_box_autostart_substitutes_each_vms_own_path() -> None:
    client = _make_client(
        {
            "mngr-slice-old1": _OLD_AUTOSTART_SCRIPT,
            "mngr-slice-new1": build_autostart_installer_script(DEFAULT_SERVICES_AGENT_PATH),
            "mngr-slice-bare": None,
        }
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    outcome_by_vm = {o.vm_name: o for o in outcomes}
    # Only mngr-slice-* instances are touched (the "not-a-slice" VM is ignored).
    assert set(outcome_by_vm.keys()) == {"mngr-slice-old1", "mngr-slice-new1", "mngr-slice-bare"}
    assert all(o.status == SliceAutostartBackfillStatus.BACKFILLED for o in outcomes)
    assert outcome_by_vm["mngr-slice-old1"].services_agent_path == _OLD_LAYOUT_PATH
    assert outcome_by_vm["mngr-slice-new1"].services_agent_path == DEFAULT_SERVICES_AGENT_PATH
    # A VM without any existing script gets the current layout.
    assert outcome_by_vm["mngr-slice-bare"].services_agent_path == DEFAULT_SERVICES_AGENT_PATH
    # The old-layout VM's installer really carries its own path.
    assert _OLD_LAYOUT_PATH in client.installed_scripts_by_vm["mngr-slice-old1"]
    assert DEFAULT_SERVICES_AGENT_PATH in client.installed_scripts_by_vm["mngr-slice-new1"]


def test_backfill_box_autostart_dry_run_applies_nothing() -> None:
    client = _make_client({"mngr-slice-old1": _OLD_AUTOSTART_SCRIPT})

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=True)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.WOULD_BACKFILL]
    assert outcomes[0].services_agent_path == _OLD_LAYOUT_PATH
    assert client.installed_scripts_by_vm == {}


def test_backfill_box_autostart_reports_a_failing_vm_with_detail() -> None:
    client = _make_client({"mngr-slice-old1": _OLD_AUTOSTART_SCRIPT}, is_install_failing=True)

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].detail == "installer exploded"


def test_backfill_box_autostart_reports_an_unreadable_vm_as_failed_without_installing() -> None:
    # The read command carries `|| true`, so a non-zero exit means the
    # limactl shell / SSH invocation itself broke (e.g. a stopped VM). That VM
    # must be reported as failed -- never rendered with a silently guessed
    # path -- and must not be applied to. Dry runs surface it the same way.
    client = _make_client({"mngr-slice-old1": _OLD_AUTOSTART_SCRIPT}, is_read_failing=True)

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].services_agent_path is None
    assert outcomes[0].detail is not None and "limactl shell exploded" in outcomes[0].detail
    assert client.installed_scripts_by_vm == {}

    dry_outcomes = backfill_box_autostart(client, "server-1", is_dry_run=True)
    assert [o.status for o in dry_outcomes] == [SliceAutostartBackfillStatus.FAILED]


def test_build_autostart_backfill_report_counts_by_status() -> None:
    outcomes = [
        SliceAutostartBackfillOutcome(
            server_id="s1", vm_name="mngr-slice-a", status=SliceAutostartBackfillStatus.BACKFILLED
        ),
        SliceAutostartBackfillOutcome(
            server_id="s1", vm_name="mngr-slice-b", status=SliceAutostartBackfillStatus.FAILED, detail="boom"
        ),
        SliceAutostartBackfillOutcome(
            server_id="s2", vm_name="mngr-slice-c", status=SliceAutostartBackfillStatus.WOULD_BACKFILL
        ),
    ]

    report = build_autostart_backfill_report(outcomes, ["s3"])

    assert report.backfilled == 1
    assert report.failed == 1
    assert report.would_backfill == 1
    assert report.unreadable_boxes == ("s3",)
    assert len(report.vms) == 3
