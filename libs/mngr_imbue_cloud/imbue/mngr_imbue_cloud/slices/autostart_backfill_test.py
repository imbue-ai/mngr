from imbue.mngr_imbue_cloud.slices.autostart_backfill import CONTAINER_START_ONLY_DETAIL
from imbue.mngr_imbue_cloud.slices.autostart_backfill import SliceAutostartBackfillOutcome
from imbue.mngr_imbue_cloud.slices.autostart_backfill import SliceAutostartBackfillStatus
from imbue.mngr_imbue_cloud.slices.autostart_backfill import VERIFY_FAILURE_MARKER
from imbue.mngr_imbue_cloud.slices.autostart_backfill import VERIFY_SUCCESS_MARKER
from imbue.mngr_imbue_cloud.slices.autostart_backfill import _AUTOSTART_INSTALLER_SCRIPT
from imbue.mngr_imbue_cloud.slices.autostart_backfill import backfill_box_autostart
from imbue.mngr_imbue_cloud.slices.autostart_backfill import build_autostart_backfill_report
from imbue.mngr_imbue_cloud.slices.autostart_backfill import build_autostart_verify_script
from imbue.mngr_imbue_cloud.slices.autostart_backfill import parse_autostart_verify_output
from imbue.mngr_imbue_cloud.slices.autostart_backfill import parse_pre_install_stamp
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

_OLD_LAYOUT_PATH = "/mngr/code/scripts/minds_start_services_agent.sh"
_CURRENT_LAYOUT_PATH = "/home/user/workspace/system/scripts/minds_start_services_agent.sh"


def test_installer_fires_the_service_explicitly_after_starting_the_path_unit() -> None:
    # Starting the path unit alone never re-runs a service the old
    # installer's boot-time oneshot left latched active (RemainAfterExit=yes),
    # so the installer must restart the service itself -- without blocking,
    # since the service body may legitimately wait on the data-volume mount.
    lines = _AUTOSTART_INSTALLER_SCRIPT.splitlines()
    assert "systemctl restart --no-block minds-autostart.service" in lines
    # The restart comes after the path unit is enabled and started.
    assert lines.index("systemctl restart --no-block minds-autostart.service") > lines.index(
        "systemctl start minds-autostart.path"
    )


def test_installer_relaunch_step_probes_every_known_script_generation() -> None:
    assert _CURRENT_LAYOUT_PATH in _AUTOSTART_INSTALLER_SCRIPT
    assert _OLD_LAYOUT_PATH in _AUTOSTART_INSTALLER_SCRIPT


def test_installer_relaunch_step_degrades_to_sshd_for_scriptless_containers() -> None:
    # A container generation without the start script must not fail the unit
    # (the path unit retriggers a failing oneshot without rate limit, which
    # hot-loops forever on a permanently missing script); it starts sshd and
    # succeeds with a journal notice instead.
    assert "/usr/sbin/sshd" in _AUTOSTART_INSTALLER_SCRIPT
    assert "no minds_start_services_agent.sh in this container generation" in _AUTOSTART_INSTALLER_SCRIPT


def test_installer_keeps_the_volume_gated_units() -> None:
    assert "PathExists=/mngr-btrfs/.minds-volume-ready" in _AUTOSTART_INSTALLER_SCRIPT
    assert "systemctl enable minds-autostart.path" in _AUTOSTART_INSTALLER_SCRIPT


def test_parse_pre_install_stamp_reads_a_stamp_and_defaults_to_zero() -> None:
    assert parse_pre_install_stamp("123456789\n") == 123456789
    assert parse_pre_install_stamp("") == 0
    assert parse_pre_install_stamp("not-a-number\n") == 0


def test_build_autostart_verify_script_embeds_the_stamp_and_markers() -> None:
    script = build_autostart_verify_script(424242)
    assert "pre=424242" in script
    assert VERIFY_SUCCESS_MARKER in script
    assert VERIFY_FAILURE_MARKER in script


def test_parse_autostart_verify_output_reads_a_clean_success() -> None:
    verdict = parse_autostart_verify_output(f"{VERIFY_SUCCESS_MARKER} notice=0\n")
    assert verdict.is_verified
    assert not verdict.is_container_start_only


def test_parse_autostart_verify_output_flags_the_scriptless_notice() -> None:
    verdict = parse_autostart_verify_output(f"{VERIFY_SUCCESS_MARKER} notice=1\n")
    assert verdict.is_verified
    assert verdict.is_container_start_only


def test_parse_autostart_verify_output_carries_the_reason_and_journal_tail_on_failure() -> None:
    stdout = f"{VERIFY_FAILURE_MARKER} the fired minds-autostart run failed\njournal line 1\njournal line 2\n"

    verdict = parse_autostart_verify_output(stdout)

    assert not verdict.is_verified
    assert verdict.detail is not None
    assert "the fired minds-autostart run failed" in verdict.detail
    assert "journal line 2" in verdict.detail


def test_parse_autostart_verify_output_treats_a_missing_marker_as_failure() -> None:
    verdict = parse_autostart_verify_output("ssh connection torn down mid-command")

    assert not verdict.is_verified
    assert verdict.detail is not None and "no verdict marker" in verdict.detail


class _ScriptedBoxClient(LimaSliceVpsClient):
    """Slice-box client double that answers the sweep's three per-VM commands from canned values."""

    vm_names: list[str] = []
    # Canned (returncode, stdout, stderr) per command kind, keyed by VM name;
    # a VM absent from a map gets that kind's default success answer.
    stamp_answer_by_vm: dict[str, tuple[int | None, str, str]] = {}
    install_answer_by_vm: dict[str, tuple[int | None, str, str]] = {}
    verify_answer_by_vm: dict[str, tuple[int | None, str, str]] = {}
    installed_vms: list[str] = []

    def list_instance_names(self) -> set[str]:
        return set(self.vm_names) | {"not-a-slice"}

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        vm_name = next(name for name in self.vm_names if name in remote_command)
        if "ExecMainStartTimestampMonotonic --value" in remote_command and VERIFY_SUCCESS_MARKER not in remote_command:
            return self.stamp_answer_by_vm.get(vm_name, (0, "0\n", ""))
        elif VERIFY_SUCCESS_MARKER in remote_command:
            return self.verify_answer_by_vm.get(vm_name, (0, f"{VERIFY_SUCCESS_MARKER} notice=0\n", ""))
        else:
            self.installed_vms.append(vm_name)
            return self.install_answer_by_vm.get(vm_name, (0, "", ""))


def _make_client(
    vm_names: list[str],
    stamp_answer_by_vm: dict[str, tuple[int | None, str, str]] | None = None,
    install_answer_by_vm: dict[str, tuple[int | None, str, str]] | None = None,
    verify_answer_by_vm: dict[str, tuple[int | None, str, str]] | None = None,
) -> _ScriptedBoxClient:
    return _ScriptedBoxClient(
        box_address="203.0.113.5",
        box_ssh_user="limahost",
        private_key_path="/dev/null",
        box_host_public_key=None,
        vm_names=vm_names,
        stamp_answer_by_vm=stamp_answer_by_vm or {},
        install_answer_by_vm=install_answer_by_vm or {},
        verify_answer_by_vm=verify_answer_by_vm or {},
        installed_vms=[],
    )


def test_backfill_box_autostart_reports_backfilled_only_after_a_verified_run() -> None:
    client = _make_client(
        ["mngr-slice-a", "mngr-slice-b"],
        stamp_answer_by_vm={"mngr-slice-b": (0, "987654\n", "")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    outcome_by_vm = {o.vm_name: o for o in outcomes}
    # Only mngr-slice-* instances are touched (the "not-a-slice" VM is ignored).
    assert set(outcome_by_vm.keys()) == {"mngr-slice-a", "mngr-slice-b"}
    assert all(o.status == SliceAutostartBackfillStatus.BACKFILLED for o in outcomes)
    assert all(o.detail is None for o in outcomes)
    assert sorted(client.installed_vms) == ["mngr-slice-a", "mngr-slice-b"]


def test_backfill_box_autostart_notes_a_container_start_only_success() -> None:
    client = _make_client(
        ["mngr-slice-old"],
        verify_answer_by_vm={"mngr-slice-old": (0, f"{VERIFY_SUCCESS_MARKER} notice=2\n", "")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.BACKFILLED]
    assert outcomes[0].detail == CONTAINER_START_ONLY_DETAIL


def test_backfill_box_autostart_fails_a_vm_whose_fired_run_fails() -> None:
    failure_stdout = f"{VERIFY_FAILURE_MARKER} the fired minds-autostart run failed\nagent container x did not start\n"
    client = _make_client(
        ["mngr-slice-a"],
        verify_answer_by_vm={"mngr-slice-a": (0, failure_stdout, "")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].detail is not None
    assert "the fired minds-autostart run failed" in outcomes[0].detail
    assert "agent container x did not start" in outcomes[0].detail


def test_backfill_box_autostart_fails_a_vm_whose_verification_command_breaks() -> None:
    # A non-zero exit from the verification command means the SSH/limactl
    # invocation itself broke (the in-VM loop always exits 0), which must be
    # reported distinctly from a failed service run.
    client = _make_client(
        ["mngr-slice-a"],
        verify_answer_by_vm={"mngr-slice-a": (1, "", "limactl shell torn down")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].detail is not None
    assert "the verification command could not run" in outcomes[0].detail
    assert "limactl shell torn down" in outcomes[0].detail
    # The installer itself did run; only the verification step broke.
    assert client.installed_vms == ["mngr-slice-a"]


def test_backfill_box_autostart_fails_a_vm_whose_installer_fails_without_verifying() -> None:
    client = _make_client(
        ["mngr-slice-a"],
        install_answer_by_vm={"mngr-slice-a": (1, "", "installer exploded")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].detail == "installer exploded"


def test_backfill_box_autostart_reports_an_unreachable_vm_as_failed_without_installing() -> None:
    # The pre-install stamp read doubles as the reachability probe: a VM whose
    # limactl shell / SSH invocation breaks (e.g. a stopped or wedged VM) is
    # reported as failed and never applied to. Dry runs surface it the same way.
    client = _make_client(
        ["mngr-slice-a"],
        stamp_answer_by_vm={"mngr-slice-a": (1, "", "limactl shell exploded")},
    )

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=False)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.FAILED]
    assert outcomes[0].detail is not None and "limactl shell exploded" in outcomes[0].detail
    assert client.installed_vms == []

    dry_outcomes = backfill_box_autostart(client, "server-1", is_dry_run=True)
    assert [o.status for o in dry_outcomes] == [SliceAutostartBackfillStatus.FAILED]


def test_backfill_box_autostart_dry_run_applies_nothing() -> None:
    client = _make_client(["mngr-slice-a"])

    outcomes = backfill_box_autostart(client, "server-1", is_dry_run=True)

    assert [o.status for o in outcomes] == [SliceAutostartBackfillStatus.WOULD_BACKFILL]
    assert client.installed_vms == []


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
