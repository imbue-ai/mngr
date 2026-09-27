"""What the local compute backends need from the machine minds runs on, and whether it is there.

The desktop app bundles every tool it can, but the local backends sit on
software only the machine's owner can install: Docker's daemon and socket
access, gVisor's ``runsc`` runtime registered with that daemon, and for Lima on
Linux the host's QEMU plus ``/dev/kvm``. On macOS the bundled ``limactl`` runs
VMs through Virtualization.framework, so Lima needs nothing there.

Each backend is probed on the create form's defaults request and reported
with the command that installs what is missing, so the form can offer it
before a create is submitted rather than fail one afterwards. Every probe is
bounded to a few seconds; a daemon that does not answer reads as absent.
"""

import json
import os
import platform
import shutil
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.primitives import LaunchMode

_PROBE_TIMEOUT_SECONDS: Final[float] = 3.0
# A probe that finishes but takes longer than this is reported: a daemon
# slowing down shows up in the logs before it stops answering in time.
_PROBE_SLOW_WARNING_SECONDS: Final[float] = 1.0
_KVM_DEVICE: Final[Path] = Path("/dev/kvm")

_DARWIN_SYSTEM: Final[str] = "Darwin"
_LINUX_SYSTEM: Final[str] = "Linux"

DOCKER_INSTALL_DOCS_URL: Final[str] = "https://docs.docker.com/engine/install/"
DOCKER_DESKTOP_MAC_DOCS_URL: Final[str] = "https://docs.docker.com/desktop/setup/install/mac-install/"
GVISOR_INSTALL_DOCS_URL: Final[str] = "https://gvisor.dev/docs/user_guide/install/"
QEMU_INSTALL_DOCS_URL: Final[str] = "https://www.qemu.org/download/#linux"

# One-liners a user can paste into a terminal on an apt-based host. Each ends
# where the machine's owner has to act next (a re-login for a new group).
_APT_DOCKER_INSTALL_COMMAND: Final[str] = (
    'curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$USER"'
)
# For a Docker that is installed but not reachable: the daemon is stopped, or
# this user is not in its group.
_APT_DOCKER_START_COMMAND: Final[str] = 'sudo systemctl start docker && sudo usermod -aG docker "$USER"'
_APT_GVISOR_INSTALL_COMMAND: Final[str] = (
    "sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl gnupg"
    " && curl -fsSL https://gvisor.dev/archive.key"
    " | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg"
    ' && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg]'
    ' https://storage.googleapis.com/gvisor/releases release main"'
    " | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null"
    " && sudo apt-get update && sudo apt-get install -y runsc"
    " && sudo runsc install -- --overlay2=none && sudo systemctl restart docker"
)
_APT_QEMU_INSTALL_COMMAND_TEMPLATE: Final[str] = (
    'sudo apt-get install -y {apt_package_name} && sudo usermod -aG kvm "$USER"'
)


class LocalBackendKey(UpperCaseStrEnum):
    """The local compute facilities the create form can select, each with its own host requirement."""

    # The Docker daemon, for ``LaunchMode.DOCKER``.
    DOCKER = auto()
    # gVisor's ``runsc`` runtime registered with that daemon, for ``DockerRuntime.RUNSC``.
    RUNSC = auto()
    # What the bundled ``limactl`` needs from the host, for ``LaunchMode.LIMA``.
    LIMA = auto()


class HostPlatform(UpperCaseStrEnum):
    """The operating systems the desktop app runs on, which decide what each local backend needs."""

    DARWIN = auto()
    LINUX = auto()


@pure
def host_platform_from_system(system: str) -> HostPlatform:
    """The platform a ``platform.system()`` value names; anything that is not macOS reads as Linux."""
    if system == _DARWIN_SYSTEM:
        return HostPlatform.DARWIN
    if system != _LINUX_SYSTEM:
        logger.debug("Treated the unrecognized platform.system() value {!r} as Linux", system)
    return HostPlatform.LINUX


class HostPackageManager(UpperCaseStrEnum):
    """Which package manager the install command can be written for."""

    APT = auto()
    NONE = auto()


class DeviceAccess(UpperCaseStrEnum):
    """Whether a device node is there for this user to open."""

    # The node does not exist: the kernel offers no such device.
    ABSENT = auto()
    # The node exists, but this user cannot read and write it.
    INACCESSIBLE = auto()
    ACCESSIBLE = auto()


class HostQemu(FrozenModel):
    """The QEMU Lima runs its VM under on a Linux host of one architecture, and the apt package shipping it."""

    executable_name: str = Field(description="The qemu-system binary Lima launches for this architecture")
    apt_package_name: str = Field(description="The Debian package that installs that binary")


# Lima picks the VM architecture from the host's, so the emulator it needs is
# the host architecture's own (mngr_lima sets the VM arch from platform.machine()).
_X86_64_QEMU: Final[HostQemu] = HostQemu(executable_name="qemu-system-x86_64", apt_package_name="qemu-system-x86")
_AARCH64_QEMU: Final[HostQemu] = HostQemu(executable_name="qemu-system-aarch64", apt_package_name="qemu-system-arm")
_QEMU_BY_MACHINE: Final[dict[str, HostQemu]] = {
    "x86_64": _X86_64_QEMU,
    "amd64": _X86_64_QEMU,
    "aarch64": _AARCH64_QEMU,
    "arm64": _AARCH64_QEMU,
}


class LocalBackendPrerequisite(FrozenModel):
    """One local backend's availability on this machine, and how to make it available."""

    key: LocalBackendKey = Field(description="The backend this describes")
    is_available: bool = Field(description="Whether a create on this backend can proceed right now")
    summary: str = Field(description="One line saying what is ready or what is missing")
    install_command: str = Field(description="A pasteable command that installs what is missing, or empty")
    docs_url: str = Field(description="Where to read about installing it by hand")


@pure
def _ready(key: LocalBackendKey, summary: str, docs_url: str) -> LocalBackendPrerequisite:
    return LocalBackendPrerequisite(key=key, is_available=True, summary=summary, install_command="", docs_url=docs_url)


@pure
def _missing(key: LocalBackendKey, summary: str, install_command: str, docs_url: str) -> LocalBackendPrerequisite:
    return LocalBackendPrerequisite(
        key=key, is_available=False, summary=summary, install_command=install_command, docs_url=docs_url
    )


class CommandProbeResult(FrozenModel):
    """What a probe command produced."""

    returncode: int = Field(description="The command's exit status")
    stdout: str = Field(description="Its standard output")


class HostProbeInterface(MutableModel, ABC):
    """The machine facts the prerequisite checks read."""

    @abstractmethod
    def platform_system(self) -> str:
        """The value of ``platform.system()``: ``Darwin`` or ``Linux``."""

    @abstractmethod
    def platform_machine(self) -> str:
        """The value of ``platform.machine()``: ``x86_64``, ``aarch64``, ``arm64``, and so on."""

    @abstractmethod
    def which(self, executable_name: str) -> Path | None:
        """Where the named executable resolves on this process's ``PATH``, or None."""

    @abstractmethod
    def run(self, command: Sequence[str]) -> CommandProbeResult | None:
        """Run a short command to completion, or return None when it could not be launched or timed out."""

    @abstractmethod
    def device_access(self, device_path: Path) -> DeviceAccess:
        """Whether the device node exists, and if so whether this user can read and write it."""


class SubprocessHostProbe(HostProbeInterface):
    """The real machine, probed through ``shutil.which`` and short subprocesses."""

    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Runs each probe command, so a probe never outlives the app that asked"
    )
    timeout_seconds: float = Field(default=_PROBE_TIMEOUT_SECONDS, frozen=True, description="Per-command budget")
    slow_warning_seconds: float = Field(
        default=_PROBE_SLOW_WARNING_SECONDS,
        frozen=True,
        description="A finished probe slower than this is warned about",
    )

    def platform_system(self) -> str:
        return platform.system()

    def platform_machine(self) -> str:
        return platform.machine()

    def which(self, executable_name: str) -> Path | None:
        resolved = shutil.which(executable_name)
        return Path(resolved) if resolved is not None else None

    def run(self, command: Sequence[str]) -> CommandProbeResult | None:
        started_at = time.monotonic()
        try:
            completed = self.concurrency_group.run_process_to_completion(
                command, timeout=self.timeout_seconds, is_checked_after=False
            )
        except ProcessError as exc:
            logger.debug("Prerequisite probe {} did not complete: {}", command[0], exc)
            return None
        if completed.is_timed_out or completed.returncode is None:
            logger.debug("Prerequisite probe {} did not finish within {}s", command[0], self.timeout_seconds)
            return None
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > self.slow_warning_seconds:
            logger.warning(
                "Prerequisite probe {} took {:.1f}s, over the {:.1f}s expected",
                command[0],
                elapsed_seconds,
                self.slow_warning_seconds,
            )
        return CommandProbeResult(returncode=completed.returncode, stdout=completed.stdout)

    def device_access(self, device_path: Path) -> DeviceAccess:
        if not device_path.exists():
            return DeviceAccess.ABSENT
        if os.access(device_path, os.R_OK | os.W_OK):
            return DeviceAccess.ACCESSIBLE
        return DeviceAccess.INACCESSIBLE


def detect_host_package_manager(probe: HostProbeInterface) -> HostPackageManager:
    if probe.which("apt-get") is not None:
        return HostPackageManager.APT
    return HostPackageManager.NONE


def _qemu_for_machine(machine: str) -> HostQemu:
    """The QEMU a Linux host of this architecture runs Lima's VM under; unknown architectures read as x86_64."""
    qemu = _QEMU_BY_MACHINE.get(machine.lower())
    if qemu is None:
        logger.debug("Assumed the x86_64 QEMU for the unrecognized host machine {}", machine)
        return _X86_64_QEMU
    return qemu


class HostFacts(FrozenModel):
    """What the prerequisite checks read once about the machine before probing each backend."""

    platform: HostPlatform = Field(description="Which operating system this machine runs")
    package_manager: HostPackageManager = Field(description="Which package manager the install commands target")
    qemu: HostQemu = Field(description="The QEMU Lima would run its VM under on this machine's architecture")


def _read_host_facts(probe: HostProbeInterface) -> HostFacts:
    return HostFacts(
        platform=host_platform_from_system(probe.platform_system()),
        package_manager=detect_host_package_manager(probe),
        qemu=_qemu_for_machine(probe.platform_machine()),
    )


def _is_docker_daemon_answering(probe: HostProbeInterface) -> bool:
    result = probe.run(["docker", "version", "--format", "{{.Server.Version}}"])
    return result is not None and result.returncode == 0


def _registered_docker_runtime_names(probe: HostProbeInterface) -> frozenset[str]:
    """The runtimes the daemon reports, or an empty set when it cannot be read."""
    result = probe.run(["docker", "info", "--format", "{{json .Runtimes}}"])
    if result is None or result.returncode != 0:
        return frozenset()
    try:
        runtimes = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse the Docker daemon's runtime list: {}", exc)
        return frozenset()
    if not isinstance(runtimes, dict):
        logger.warning("Ignored the Docker daemon's runtime list: expected a mapping, got {}", type(runtimes).__name__)
        return frozenset()
    return frozenset(str(name) for name in runtimes)


def _linux_install_command_for(key: LocalBackendKey, host: HostFacts) -> str:
    """The pasteable command that installs a missing backend on a Linux host, or empty without apt."""
    match host.package_manager:
        case HostPackageManager.NONE:
            return ""
        case HostPackageManager.APT:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    match key:
        case LocalBackendKey.DOCKER:
            return _APT_DOCKER_INSTALL_COMMAND
        case LocalBackendKey.RUNSC:
            return _APT_GVISOR_INSTALL_COMMAND
        case LocalBackendKey.LIMA:
            return _APT_QEMU_INSTALL_COMMAND_TEMPLATE.format(apt_package_name=host.qemu.apt_package_name)
        case _ as unreachable:
            assert_never(unreachable)


def _darwin_docker_prerequisite(probe: HostProbeInterface) -> LocalBackendPrerequisite:
    # Docker Desktop is the whole install on macOS, and there is no docker
    # group to join there.
    is_installed = probe.which("docker") is not None
    if is_installed and _is_docker_daemon_answering(probe):
        return _ready(LocalBackendKey.DOCKER, "Docker is running.", DOCKER_DESKTOP_MAC_DOCS_URL)
    summary = (
        "Docker is installed but its daemon is not answering. Start Docker Desktop."
        if is_installed
        else "Docker is not installed. Install Docker Desktop and start it."
    )
    return _missing(LocalBackendKey.DOCKER, summary, "", DOCKER_DESKTOP_MAC_DOCS_URL)


def _linux_docker_prerequisite(probe: HostProbeInterface, host: HostFacts) -> LocalBackendPrerequisite:
    is_installed = probe.which("docker") is not None
    if is_installed and _is_docker_daemon_answering(probe):
        return _ready(LocalBackendKey.DOCKER, "Docker is running.", DOCKER_INSTALL_DOCS_URL)
    if is_installed:
        # Installed already, so the pasteable command starts it and joins its
        # group rather than running the installer again.
        start_command = _APT_DOCKER_START_COMMAND if host.package_manager is HostPackageManager.APT else ""
        return _missing(
            LocalBackendKey.DOCKER,
            "Docker is installed but its daemon is not answering. Start Docker, and make sure your user is in the docker group.",
            start_command,
            DOCKER_INSTALL_DOCS_URL,
        )
    return _missing(
        LocalBackendKey.DOCKER,
        "Docker is not installed. Install Docker Engine, then log out and back in so your user can reach it.",
        _linux_install_command_for(LocalBackendKey.DOCKER, host),
        DOCKER_INSTALL_DOCS_URL,
    )


def _docker_prerequisite(probe: HostProbeInterface, host: HostFacts) -> LocalBackendPrerequisite:
    match host.platform:
        case HostPlatform.DARWIN:
            return _darwin_docker_prerequisite(probe)
        case HostPlatform.LINUX:
            return _linux_docker_prerequisite(probe, host)
        case _ as unreachable:
            assert_never(unreachable)


def _linux_runsc_prerequisite(
    probe: HostProbeInterface, docker: LocalBackendPrerequisite, host: HostFacts
) -> LocalBackendPrerequisite:
    command = _linux_install_command_for(LocalBackendKey.RUNSC, host)
    if not docker.is_available:
        return _missing(
            LocalBackendKey.RUNSC,
            "gVisor (runsc) needs a running Docker daemon to register with.",
            command,
            GVISOR_INSTALL_DOCS_URL,
        )
    if "runsc" in _registered_docker_runtime_names(probe):
        return _ready(LocalBackendKey.RUNSC, "gVisor (runsc) is registered with Docker.", GVISOR_INSTALL_DOCS_URL)
    return _missing(
        LocalBackendKey.RUNSC,
        "gVisor (runsc) is not registered with Docker. Install it and register it with --overlay2=none.",
        command,
        GVISOR_INSTALL_DOCS_URL,
    )


def _runsc_prerequisite(
    probe: HostProbeInterface, docker: LocalBackendPrerequisite, host: HostFacts
) -> LocalBackendPrerequisite:
    match host.platform:
        case HostPlatform.DARWIN:
            return _missing(
                LocalBackendKey.RUNSC,
                "gVisor (runsc) is not available on macOS; the Docker VM is the isolation boundary there.",
                "",
                GVISOR_INSTALL_DOCS_URL,
            )
        case HostPlatform.LINUX:
            return _linux_runsc_prerequisite(probe, docker, host)
        case _ as unreachable:
            assert_never(unreachable)


def _lima_prerequisite(probe: HostProbeInterface, host: HostFacts) -> LocalBackendPrerequisite:
    match host.platform:
        case HostPlatform.DARWIN:
            return _ready(
                LocalBackendKey.LIMA,
                "Lima runs through Virtualization.framework; nothing to install.",
                QEMU_INSTALL_DOCS_URL,
            )
        case HostPlatform.LINUX:
            return _linux_lima_prerequisite(probe, host)
        case _ as unreachable:
            assert_never(unreachable)


def _linux_lima_prerequisite(probe: HostProbeInterface, host: HostFacts) -> LocalBackendPrerequisite:
    command = _linux_install_command_for(LocalBackendKey.LIMA, host)
    missing: list[str] = []
    if probe.which(host.qemu.executable_name) is None:
        missing.append(f"QEMU ({host.qemu.executable_name}) is not installed")
    kvm_access = probe.device_access(_KVM_DEVICE)
    match kvm_access:
        case DeviceAccess.ACCESSIBLE:
            pass
        case DeviceAccess.INACCESSIBLE:
            missing.append(
                f"{_KVM_DEVICE} is not accessible to your user (join the kvm group, then log out and back in)"
            )
        case DeviceAccess.ABSENT:
            missing.append(
                f"{_KVM_DEVICE} does not exist, so this machine offers no KVM"
                " (hardware virtualization is off or unsupported here)"
            )
        case _ as unreachable:
            assert_never(unreachable)
    if not missing:
        return _ready(LocalBackendKey.LIMA, "QEMU and KVM are available for Lima.", QEMU_INSTALL_DOCS_URL)
    # No command adds a device the kernel does not offer, so a missing KVM
    # leaves only the docs, whatever else could be installed.
    return _missing(
        LocalBackendKey.LIMA,
        "Lima on Linux runs its VM through the host's QEMU with KVM. " + "; ".join(missing) + ".",
        command if kvm_access is not DeviceAccess.ABSENT else "",
        QEMU_INSTALL_DOCS_URL,
    )


def probe_local_prerequisites(probe: HostProbeInterface) -> tuple[LocalBackendPrerequisite, ...]:
    """Every local backend's availability, in the order the form lists them."""
    host = _read_host_facts(probe)
    docker = _docker_prerequisite(probe, host)
    return (
        docker,
        _runsc_prerequisite(probe, docker, host),
        _lima_prerequisite(probe, host),
    )


_PREREQUISITE_KEY_BY_LOCAL_LAUNCH_MODE: Final[dict[LaunchMode, LocalBackendKey]] = {
    LaunchMode.DOCKER: LocalBackendKey.DOCKER,
    LaunchMode.LIMA: LocalBackendKey.LIMA,
}


@pure
def _local_launch_mode_preference(host_platform: HostPlatform) -> tuple[LaunchMode, ...]:
    """The local launch modes the create form's local preset prefers on this platform, most preferred first.

    macOS prefers Lima (self-contained); Linux prefers Docker, which is what
    most Linux machines already have.
    """
    match host_platform:
        case HostPlatform.DARWIN:
            return (LaunchMode.LIMA, LaunchMode.DOCKER)
        case HostPlatform.LINUX:
            return (LaunchMode.DOCKER, LaunchMode.LIMA)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def local_launch_mode_for(
    host_platform: HostPlatform, prerequisites: Sequence[LocalBackendPrerequisite]
) -> LaunchMode:
    """The launch mode the local preset selects: the first ready one in the platform's order, else the first."""
    preference = _local_launch_mode_preference(host_platform)
    available_keys = {entry.key for entry in prerequisites if entry.is_available}
    for launch_mode in preference:
        if _PREREQUISITE_KEY_BY_LOCAL_LAUNCH_MODE[launch_mode] in available_keys:
            return launch_mode
    return preference[0]
