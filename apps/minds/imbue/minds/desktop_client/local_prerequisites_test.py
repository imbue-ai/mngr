from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.local_prerequisites import CommandProbeResult
from imbue.minds.desktop_client.local_prerequisites import DeviceAccess
from imbue.minds.desktop_client.local_prerequisites import HostPackageManager
from imbue.minds.desktop_client.local_prerequisites import HostPlatform
from imbue.minds.desktop_client.local_prerequisites import LocalBackendKey
from imbue.minds.desktop_client.local_prerequisites import LocalBackendPrerequisite
from imbue.minds.desktop_client.local_prerequisites import SubprocessHostProbe
from imbue.minds.desktop_client.local_prerequisites import detect_host_package_manager
from imbue.minds.desktop_client.local_prerequisites import host_platform_from_system
from imbue.minds.desktop_client.local_prerequisites import local_launch_mode_for
from imbue.minds.desktop_client.local_prerequisites import probe_local_prerequisites
from imbue.minds.desktop_client.mock_local_prerequisites_test import FakeHostProbe
from imbue.minds.primitives import LaunchMode

_DOCKER_VERSION_PROBE = "docker version --format {{.Server.Version}}"
_DOCKER_RUNTIMES_PROBE = "docker info --format {{json .Runtimes}}"
_ANSWERING = CommandProbeResult(returncode=0, stdout="28.1.0\n")
_RUNTIMES_WITH_RUNSC = CommandProbeResult(
    returncode=0, stdout='{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}\n'
)
_RUNTIMES_WITHOUT_RUNSC = CommandProbeResult(returncode=0, stdout='{"runc":{"path":"runc"}}\n')
_KVM_ACCESSIBLE = {"/dev/kvm": DeviceAccess.ACCESSIBLE}
# A fresh machine with virtualization on: the node is there, this user is not in its group yet.
_KVM_LOCKED = {"/dev/kvm": DeviceAccess.INACCESSIBLE}


def _by_key(probe: FakeHostProbe) -> dict[LocalBackendKey, LocalBackendPrerequisite]:
    return {entry.key: entry for entry in probe_local_prerequisites(probe)}


def test_a_fresh_linux_machine_is_told_what_to_install_for_every_backend() -> None:
    probe = FakeHostProbe(system="Linux", executables=frozenset({"apt-get"}), access_by_device=_KVM_LOCKED)

    docker, runsc, lima = probe_local_prerequisites(probe)

    assert docker.key is LocalBackendKey.DOCKER and not docker.is_available
    assert "not installed" in docker.summary
    assert docker.install_command.startswith("curl -fsSL https://get.docker.com")
    assert runsc.key is LocalBackendKey.RUNSC and not runsc.is_available
    assert "needs a running Docker daemon" in runsc.summary
    assert "--overlay2=none" in runsc.install_command
    # The same apt source serves both architectures a Linux host can be (x86_64 and a Pi).
    assert "[arch=amd64,arm64 " in runsc.install_command
    assert lima.key is LocalBackendKey.LIMA and not lima.is_available
    assert "qemu-system-x86_64" in lima.summary and "/dev/kvm" in lima.summary
    assert lima.install_command.startswith("sudo apt-get install -y qemu-system-x86")
    # Nothing was run: without a docker binary there is no daemon to ask.
    assert probe.commands_run == []


def test_an_installed_docker_whose_daemon_does_not_answer_is_reported_as_such() -> None:
    probe = FakeHostProbe(
        system="Linux",
        executables=frozenset({"apt-get", "docker"}),
        result_by_command={_DOCKER_VERSION_PROBE: CommandProbeResult(returncode=1, stdout="")},
    )

    docker = _by_key(probe)[LocalBackendKey.DOCKER]

    assert not docker.is_available
    assert "daemon is not answering" in docker.summary
    # The command matches the summary: start what is installed, not install it again.
    assert docker.install_command == 'sudo systemctl start docker && sudo usermod -aG docker "$USER"'
    # A probe that never returned (timed out or failed to launch) reads the same way,
    # and without apt there is no command to paste.
    silent = FakeHostProbe(system="Linux", executables=frozenset({"docker"}), result_by_command={})
    silent_docker = _by_key(silent)[LocalBackendKey.DOCKER]
    assert not silent_docker.is_available
    assert silent_docker.install_command == ""


def test_runsc_is_available_only_when_the_daemon_lists_it() -> None:
    registered = FakeHostProbe(
        system="Linux",
        executables=frozenset({"docker"}),
        result_by_command={_DOCKER_VERSION_PROBE: _ANSWERING, _DOCKER_RUNTIMES_PROBE: _RUNTIMES_WITH_RUNSC},
    )
    unregistered = FakeHostProbe(
        system="Linux",
        executables=frozenset({"docker"}),
        result_by_command={_DOCKER_VERSION_PROBE: _ANSWERING, _DOCKER_RUNTIMES_PROBE: _RUNTIMES_WITHOUT_RUNSC},
    )

    assert _by_key(registered)[LocalBackendKey.RUNSC].is_available
    assert _by_key(registered)[LocalBackendKey.DOCKER].is_available
    runsc = _by_key(unregistered)[LocalBackendKey.RUNSC]
    assert not runsc.is_available
    assert "not registered" in runsc.summary
    # No apt here, so there is no command to paste -- only the docs.
    assert runsc.install_command == ""
    assert runsc.docs_url.startswith("https://gvisor.dev/")


@pytest.mark.parametrize("runtime_list", ["not json", '["runsc"]'])
def test_an_unparseable_or_misshapen_runtime_list_reads_as_no_runsc(runtime_list: str) -> None:
    probe = FakeHostProbe(
        system="Linux",
        executables=frozenset({"docker"}),
        result_by_command={
            _DOCKER_VERSION_PROBE: _ANSWERING,
            _DOCKER_RUNTIMES_PROBE: CommandProbeResult(returncode=0, stdout=runtime_list),
        },
    )
    assert not _by_key(probe)[LocalBackendKey.RUNSC].is_available


def test_lima_on_linux_needs_qemu_and_an_accessible_kvm_device() -> None:
    ready = FakeHostProbe(
        system="Linux", executables=frozenset({"qemu-system-x86_64"}), access_by_device=_KVM_ACCESSIBLE
    )
    kvm_locked = FakeHostProbe(
        system="Linux", executables=frozenset({"qemu-system-x86_64", "apt-get"}), access_by_device=_KVM_LOCKED
    )
    no_kvm = FakeHostProbe(system="Linux", executables=frozenset({"qemu-system-x86_64", "apt-get"}))

    assert _by_key(ready)[LocalBackendKey.LIMA].is_available
    # The node is there but closed to this user: joining its group is the fix.
    lima = _by_key(kvm_locked)[LocalBackendKey.LIMA]
    assert not lima.is_available
    assert "kvm group" in lima.summary and "qemu-system-x86_64" not in lima.summary
    assert "kvm" in lima.install_command
    # No node at all: the kernel offers no KVM, and no command can add one.
    lima_without_kvm = _by_key(no_kvm)[LocalBackendKey.LIMA]
    assert not lima_without_kvm.is_available
    assert "does not exist" in lima_without_kvm.summary and "kvm group" not in lima_without_kvm.summary
    assert lima_without_kvm.install_command == ""


def test_lima_on_an_arm64_linux_host_looks_for_the_aarch64_qemu() -> None:
    ready = FakeHostProbe(
        system="Linux",
        machine="aarch64",
        executables=frozenset({"qemu-system-aarch64"}),
        access_by_device=_KVM_ACCESSIBLE,
    )
    # The x86 emulator is installed, but Lima on this host runs the aarch64 one.
    wrong_qemu = FakeHostProbe(
        system="Linux",
        machine="aarch64",
        executables=frozenset({"apt-get", "qemu-system-x86_64"}),
        access_by_device=_KVM_ACCESSIBLE,
    )

    assert _by_key(ready)[LocalBackendKey.LIMA].is_available
    lima = _by_key(wrong_qemu)[LocalBackendKey.LIMA]
    assert not lima.is_available
    assert "qemu-system-aarch64" in lima.summary
    assert lima.install_command.startswith("sudo apt-get install -y qemu-system-arm ")


def test_an_unrecognized_host_machine_is_probed_as_x86_64() -> None:
    probe = FakeHostProbe(
        system="Linux", machine="riscv64", executables=frozenset({"apt-get"}), access_by_device=_KVM_LOCKED
    )

    lima = _by_key(probe)[LocalBackendKey.LIMA]

    assert "qemu-system-x86_64" in lima.summary
    assert lima.install_command.startswith("sudo apt-get install -y qemu-system-x86 ")


def test_macos_has_lima_built_in_and_never_offers_runsc() -> None:
    probe = FakeHostProbe(system="Darwin", executables=frozenset())

    by_key = _by_key(probe)

    assert by_key[LocalBackendKey.LIMA].is_available
    assert not by_key[LocalBackendKey.RUNSC].is_available
    assert "macOS" in by_key[LocalBackendKey.RUNSC].summary
    docker = by_key[LocalBackendKey.DOCKER]
    assert not docker.is_available
    assert docker.install_command == ""
    assert "mac" in docker.docs_url
    # Docker Desktop is the install on macOS; the docker group is a Linux thing.
    assert "Docker Desktop" in docker.summary and "docker group" not in docker.summary
    installed = FakeHostProbe(system="Darwin", executables=frozenset({"docker"}))
    assert "Start Docker Desktop" in _by_key(installed)[LocalBackendKey.DOCKER].summary


def test_the_package_manager_is_apt_only_when_apt_get_resolves() -> None:
    assert detect_host_package_manager(FakeHostProbe(executables=frozenset({"apt-get"}))) is HostPackageManager.APT
    assert detect_host_package_manager(FakeHostProbe(executables=frozenset({"dnf"}))) is HostPackageManager.NONE


def test_every_platform_system_value_other_than_darwin_reads_as_linux() -> None:
    assert host_platform_from_system("Darwin") is HostPlatform.DARWIN
    assert host_platform_from_system("Linux") is HostPlatform.LINUX
    # The checks only distinguish macOS from everything else, so an unexpected
    # value (a Windows or Java Python, or an empty string) gets the Linux checks.
    assert host_platform_from_system("Windows") is HostPlatform.LINUX
    assert host_platform_from_system("") is HostPlatform.LINUX


def test_the_local_preset_picks_the_first_ready_backend_in_the_platform_order() -> None:
    docker_only = FakeHostProbe(
        system="Linux", executables=frozenset({"docker"}), result_by_command={_DOCKER_VERSION_PROBE: _ANSWERING}
    )
    lima_only = FakeHostProbe(
        system="Linux", executables=frozenset({"qemu-system-x86_64"}), access_by_device=_KVM_ACCESSIBLE
    )
    nothing = FakeHostProbe(system="Linux")
    mac = FakeHostProbe(system="Darwin")

    assert local_launch_mode_for(HostPlatform.LINUX, probe_local_prerequisites(docker_only)) is LaunchMode.DOCKER
    assert local_launch_mode_for(HostPlatform.LINUX, probe_local_prerequisites(lima_only)) is LaunchMode.LIMA
    # Nothing ready: the platform's first choice, so the form still has a selection to annotate.
    assert local_launch_mode_for(HostPlatform.LINUX, probe_local_prerequisites(nothing)) is LaunchMode.DOCKER
    assert local_launch_mode_for(HostPlatform.DARWIN, probe_local_prerequisites(mac)) is LaunchMode.LIMA


def test_the_subprocess_probe_reports_a_finished_command_and_folds_the_rest_into_none(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The real probe: a finished command keeps its exit status and output; anything else reads as None.

    The checks treat "could not be launched" and "did not finish in time" the
    same way as a daemon that answered with a failure, so both have to come
    back as None rather than raise into the form-defaults request.
    """
    probe = SubprocessHostProbe(concurrency_group=root_concurrency_group, timeout_seconds=0.2)

    finished = probe.run(["sh", "-c", "echo answered; exit 3"])
    assert finished == CommandProbeResult(returncode=3, stdout="answered\n")
    assert probe.run(["minds-test-no-such-executable-9b1f"]) is None
    assert probe.run(["sleep", "41263"]) is None


def test_the_subprocess_probe_reads_path_and_device_access_from_the_real_machine(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    probe = SubprocessHostProbe(concurrency_group=root_concurrency_group)
    writable = tmp_path / "kvm"
    writable.write_bytes(b"")

    assert probe.which("sh") is not None
    assert probe.which("minds-test-no-such-executable-9b1f") is None
    assert probe.device_access(writable) is DeviceAccess.ACCESSIBLE
    assert probe.device_access(tmp_path / "missing") is DeviceAccess.ABSENT
