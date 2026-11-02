"""Kernel security: version parsing, the floor, the machine script's wire format, and which machines are probed."""

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from imbue.minds.desktop_client.kernel_security import KERNEL_SECURITY_FLOOR_PACKAGE_VERSION
from imbue.minds.desktop_client.kernel_security import KernelMachineAccess
from imbue.minds.desktop_client.kernel_security import KernelProbe
from imbue.minds.desktop_client.kernel_security import KernelProbeTransport
from imbue.minds.desktop_client.kernel_security import KernelRestartMethod
from imbue.minds.desktop_client.kernel_security import KernelScriptStatus
from imbue.minds.desktop_client.kernel_security import build_machine_command
from imbue.minds.desktop_client.kernel_security import build_machine_exec_args
from imbue.minds.desktop_client.kernel_security import classify_kernel
from imbue.minds.desktop_client.kernel_security import is_kernel_fix_offered
from imbue.minds.desktop_client.kernel_security import is_running_kernel_at_least
from imbue.minds.desktop_client.kernel_security import kernel_machine_access_for_provider
from imbue.minds.desktop_client.kernel_security import machine_stdout_from_exec_json
from imbue.minds.desktop_client.kernel_security import parse_machine_script_output
from imbue.minds.desktop_client.kernel_security import restarted_machine_shortfall
from imbue.minds.desktop_client.kernel_security import run_machine_script
from imbue.minds.desktop_client.kernel_security import upstream_kernel_version
from imbue.minds.desktop_client.testing import FIXED_KERNEL_NAME
from imbue.minds.desktop_client.testing import KERNEL_METAPACKAGE_NAME
from imbue.minds.desktop_client.testing import VULNERABLE_KERNEL_NAME
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import machine_script_output
from imbue.minds.desktop_client.update_status import KernelSecurityStatus
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller

_FLOOR = "6.12.113-1"


def _probe(running: str, installed: str = "", boot_id: str = "boot-1") -> KernelProbe:
    return KernelProbe(
        running_kernel=running,
        boot_id=boot_id,
        metapackage=KERNEL_METAPACKAGE_NAME,
        installed_package_version=installed,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6.12.113+deb13-cloud-arm64", (6, 12, 113)),
        ("6.12.113-1", (6, 12, 113)),
        ("6.1.190-1", (6, 1, 190)),
        ("7.0.0-rc1", (7, 0, 0)),
        ("", ()),
        ("not-a-kernel", ()),
    ],
)
def test_the_upstream_version_is_what_a_kernel_name_and_its_package_share(
    text: str, expected: tuple[int, ...]
) -> None:
    assert upstream_kernel_version(text) == expected


@pytest.mark.parametrize(
    ("running", "is_at_least"),
    [
        (VULNERABLE_KERNEL_NAME, False),
        (FIXED_KERNEL_NAME, True),
        ("6.12.120+deb13-cloud-amd64", True),
        ("6.13.1+deb13-cloud-amd64", True),
        ("6.1.190+deb12-cloud-amd64", False),
        ("", False),
    ],
)
def test_a_kernel_is_at_least_the_floor_by_its_upstream_version(running: str, is_at_least: bool) -> None:
    assert is_running_kernel_at_least(running, _FLOOR) is is_at_least


def test_the_floor_names_the_fixed_debian_kernel() -> None:
    assert upstream_kernel_version(KERNEL_SECURITY_FLOOR_PACKAGE_VERSION) == (6, 12, 113)


@pytest.mark.parametrize(
    ("probe", "status"),
    [
        (None, KernelSecurityStatus.UNKNOWN),
        (_probe("garbage"), KernelSecurityStatus.UNKNOWN),
        (_probe(VULNERABLE_KERNEL_NAME), KernelSecurityStatus.VULNERABLE),
        (_probe(FIXED_KERNEL_NAME), KernelSecurityStatus.FIXED),
    ],
)
def test_a_machine_is_classified_against_the_floor_only_from_a_kernel_it_named(
    probe: KernelProbe | None, status: KernelSecurityStatus
) -> None:
    assert classify_kernel(probe, _FLOOR) is status


_CYCLED_OUTER = KernelMachineAccess(transport=KernelProbeTransport.OUTER, restart=KernelRestartMethod.HOST_CYCLE)


@pytest.mark.parametrize(
    ("provider_name", "access"),
    [
        ("lima", KernelMachineAccess(transport=KernelProbeTransport.AGENT, restart=KernelRestartMethod.HOST_CYCLE)),
        # A Vultr VPS runs on through its container's stop, so only it is rebooted in place.
        (
            "vultr",
            KernelMachineAccess(transport=KernelProbeTransport.OUTER, restart=KernelRestartMethod.IN_PLACE_REBOOT),
        ),
        ("aws_" + "a" * 32, _CYCLED_OUTER),
        ("gcp_" + "b" * 32, _CYCLED_OUTER),
        ("azure_" + "c" * 32, _CYCLED_OUTER),
        ("modal_" + "d" * 32, None),
        ("imbue_cloud_someone", None),
        ("docker", None),
        ("local", None),
        (None, None),
    ],
)
def test_only_the_machines_the_app_holds_root_on_are_probed_and_each_is_restarted_its_providers_way(
    provider_name: str | None, access: KernelMachineAccess | None
) -> None:
    assert kernel_machine_access_for_provider(provider_name) == access


def test_the_machine_command_carries_the_script_and_its_arguments_intact() -> None:
    command = build_machine_command("install", _FLOOR)

    encoded = command.split("'")[1]
    script = base64.b64decode(encoded).decode("utf-8")
    assert "dpkg --compare-versions" in script
    assert "systemd-run" in script
    assert command.endswith(f"| base64 -d | bash -s -- install {_FLOOR}")


def _write_stub(stubs_dir: Path, name: str, body: str) -> None:
    stub = stubs_dir / name
    stub.write_text("#!/bin/bash\n" + body + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize(
    ("installed_metapackages", "expected_metapackage", "expected_installed"),
    [
        # A stock Debian install tracks the generic flavour; the cloud images and Lima track cloud.
        ({"linux-image-amd64": "6.12.96-1"}, "linux-image-amd64", "6.12.96-1"),
        (
            {"linux-image-cloud-amd64": "6.12.113-1", "linux-image-amd64": "6.12.96-1"},
            "linux-image-cloud-amd64",
            "6.12.113-1",
        ),
        ({}, "linux-image-cloud-amd64", ""),
    ],
)
def test_the_probe_reads_the_metapackage_the_machine_already_tracks(
    tmp_path: Path, installed_metapackages: dict[str, str], expected_metapackage: str, expected_installed: str
) -> None:
    stubs_dir = tmp_path / "bin"
    stubs_dir.mkdir()
    _write_stub(stubs_dir, "dpkg", "echo amd64")
    _write_stub(stubs_dir, "uname", "echo 6.12.96+deb13-amd64")
    cases = "".join(
        f"{name}) status=installed; version={version};; " for name, version in installed_metapackages.items()
    )
    # dpkg-query -W -f=<format> <package>: a package dpkg does not know is an error, as it is for the real one.
    _write_stub(
        stubs_dir,
        "dpkg-query",
        f'case "$3" in {cases}*) exit 1;; esac\n'
        'case "$2" in *Status*) printf %s "$status";; *Version*) printf %s "$version";; esac',
    )

    completed = subprocess.run(
        ["bash", "-c", build_machine_command("probe", _FLOOR)],
        env={"PATH": f"{stubs_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    answer = parse_machine_script_output(completed.stdout)
    assert answer.status is None
    assert answer.probe is not None
    assert answer.probe.running_kernel == "6.12.96+deb13-amd64"
    assert answer.probe.metapackage == expected_metapackage
    assert answer.probe.installed_package_version == expected_installed


def test_an_agent_machine_is_reached_by_a_plain_exec_and_an_outer_one_through_the_outer_host() -> None:
    agent_args = build_machine_exec_args(KernelProbeTransport.AGENT, "ws@host.lima", "cmd")
    outer_args = build_machine_exec_args(KernelProbeTransport.OUTER, "ws@host.vultr", "cmd")

    assert agent_args == ["exec", "--no-start", "ws@host.lima", "cmd", "--format", "json"]
    assert "--outer" in outer_args
    assert "--no-start" in outer_args
    assert outer_args[outer_args.index("--missing-outer") + 1] == "abort"
    # The machine is never started to be probed: a stopped one is unknown until it runs.
    assert "--no-start" in agent_args


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (exec_json_envelope("hello\n"), "hello\n"),
        (exec_json_envelope("outer\n", results_key="outer_results"), "outer\n"),
        (json.dumps({"results": []}), None),
        ("not json", None),
        (json.dumps(["list"]), None),
    ],
)
def test_the_machine_stdout_is_unwrapped_from_either_exec_envelope(stdout: str, expected: str | None) -> None:
    assert machine_stdout_from_exec_json(stdout) == expected


def test_a_bare_probe_reads_as_measurements_without_a_verdict() -> None:
    answer = parse_machine_script_output(
        "installing...\n" + machine_script_output(VULNERABLE_KERNEL_NAME, boot_id="boot-7", installed="6.12.113-1")
    )

    assert answer.status is None
    assert answer.probe == _probe(VULNERABLE_KERNEL_NAME, "6.12.113-1", boot_id="boot-7")


def test_a_verdict_carries_its_detail_decoded() -> None:
    answer = parse_machine_script_output(
        machine_script_output(VULNERABLE_KERNEL_NAME, status="installed", detail="installed linux-image 6.12.113-1")
    )

    assert answer.status is KernelScriptStatus.INSTALLED
    assert answer.detail == "installed linux-image 6.12.113-1"
    assert answer.probe is not None


def test_an_unrecognized_verdict_and_a_missing_marker_both_read_as_failed() -> None:
    unrecognized = parse_machine_script_output(
        machine_script_output(VULNERABLE_KERNEL_NAME, status="exploded", detail="boom")
    )
    silent = parse_machine_script_output("bash: line 1: syntax error\n")

    assert unrecognized.status is KernelScriptStatus.FAILED
    assert "exploded" in unrecognized.detail
    assert unrecognized.probe is not None
    assert silent.status is KernelScriptStatus.FAILED
    assert silent.probe is None
    assert "syntax error" in silent.detail


@pytest.mark.parametrize(
    ("status", "transport", "probe", "version", "is_offered"),
    [
        (
            KernelSecurityStatus.VULNERABLE,
            KernelProbeTransport.AGENT,
            _probe(VULNERABLE_KERNEL_NAME),
            "minds-v0.9.1",
            True,
        ),
        (
            KernelSecurityStatus.VULNERABLE,
            KernelProbeTransport.OUTER,
            _probe(VULNERABLE_KERNEL_NAME),
            "minds-v0.9.0",
            True,
        ),
        # Too old a template for apt to find the package, unless it is already installed.
        (
            KernelSecurityStatus.VULNERABLE,
            KernelProbeTransport.AGENT,
            _probe(VULNERABLE_KERNEL_NAME),
            "minds-v0.8.7",
            False,
        ),
        (
            KernelSecurityStatus.VULNERABLE,
            KernelProbeTransport.AGENT,
            _probe(VULNERABLE_KERNEL_NAME, "6.12.113-1"),
            "minds-v0.8.7",
            True,
        ),
        (KernelSecurityStatus.VULNERABLE, KernelProbeTransport.AGENT, _probe(VULNERABLE_KERNEL_NAME), "", False),
        (KernelSecurityStatus.VULNERABLE, None, _probe(VULNERABLE_KERNEL_NAME), "minds-v0.9.1", False),
        (KernelSecurityStatus.FIXED, KernelProbeTransport.AGENT, _probe(FIXED_KERNEL_NAME), "minds-v0.9.1", False),
        (KernelSecurityStatus.UNKNOWN, KernelProbeTransport.AGENT, None, "minds-v0.9.1", False),
    ],
)
def test_the_fix_is_offered_where_the_app_can_install_or_has_installed_it(
    status: KernelSecurityStatus,
    transport: KernelProbeTransport | None,
    probe: KernelProbe | None,
    version: str,
    is_offered: bool,
) -> None:
    assert is_kernel_fix_offered(status, transport, probe, version, _FLOOR) is is_offered


def test_a_restart_is_verified_by_a_new_boot_on_a_fixed_kernel() -> None:
    assert restarted_machine_shortfall(_probe(FIXED_KERNEL_NAME, boot_id="boot-2"), _FLOOR, "boot-1") is None
    not_yet = restarted_machine_shortfall(_probe(FIXED_KERNEL_NAME, boot_id="boot-1"), _FLOOR, "boot-1")
    still_old = restarted_machine_shortfall(_probe(VULNERABLE_KERNEL_NAME, boot_id="boot-2"), _FLOOR, "boot-1")
    assert not_yet is not None and "not restarted" in not_yet
    assert still_old is not None and VULNERABLE_KERNEL_NAME in still_old


def test_running_the_script_reads_the_machine_through_the_exec_envelope() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=exec_json_envelope(
                machine_script_output(VULNERABLE_KERNEL_NAME, status="installed", detail="done")
            ),
        )
    )

    answer = run_machine_script(
        caller,
        KernelProbeTransport.AGENT,
        "ws@host.lima",
        "install",
        floor_package_version=_FLOOR,
        timeout_seconds=5.0,
    )

    assert answer.status is KernelScriptStatus.INSTALLED
    assert answer.probe is not None and answer.probe.running_kernel == VULNERABLE_KERNEL_NAME
    (argv,) = caller.calls
    assert argv[:3] == ["exec", "--no-start", "ws@host.lima"]


@pytest.mark.parametrize(
    ("result", "is_machine_reached"),
    [
        (host_offline_exec_result(), False),
        (MngrCallResult(returncode=255, stderr="ssh: connect to host refused", is_mngr_output=True), False),
        (MngrCallResult(returncode=-1, stderr="mngr exec timed out after 900s", is_timed_out=True), True),
        (MngrCallResult(returncode=0, stdout=json.dumps({"results": []}), is_mngr_output=True), True),
    ],
)
def test_an_exec_that_did_not_answer_is_a_failed_answer_with_no_probe_that_says_whether_the_machine_was_reached(
    result: MngrCallResult, is_machine_reached: bool
) -> None:
    """Only mngr's own refusal before the machine reads as unreached; a timed-out exec may still be running there."""
    answer = run_machine_script(
        RecordingMngrCaller(result=result),
        KernelProbeTransport.OUTER,
        "ws@host.vultr",
        "probe",
        floor_package_version=_FLOOR,
        timeout_seconds=5.0,
    )

    assert answer.status is KernelScriptStatus.FAILED
    assert answer.probe is None
    assert "could not reach the machine" in answer.detail
    assert answer.is_machine_reached is is_machine_reached
