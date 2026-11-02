"""Which kernel a workspace's machine runs, and the script that moves it onto a fixed one.

A workspace container has no kernel of its own: the kernel is the machine's, and
a kernel security fix reaches it only when the machine boots a fixed kernel. The
machines the app can act on are the ones it created and holds root on: a Lima VM,
where the agent itself runs as root in the VM, and the VPS of a cloud connection,
reached as root through ``mngr exec --outer``. Debian's unattended-upgrades
installs a fixed kernel package on such a machine but never reboots it, so the
fix sits installed and inactive until something restarts the machine.

The floor is the kernel package a machine must run at or above, and it moves
only when a kernel advisory does; the app's own template security floor
(``SECURITY_FLOOR_VERSION``) is a different axis, since updating the template
changes nothing about the kernel.
"""

import base64
import binascii
import json
import re
import shlex
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.host_access_material import HostAccessLayout
from imbue.minds.desktop_client.host_access_material import VULTR_PROVIDER_NAME
from imbue.minds.desktop_client.host_access_material import layout_for_provider_kind
from imbue.minds.desktop_client.minds_version import parse_minds_version
from imbue.minds.desktop_client.update_status import KernelSecurityStatus
from imbue.minds.utils.mngr_caller import MngrCaller

# The Debian kernel package version a machine must run at or above: trixie's
# ``linux`` 6.12.113-1 (DSA-6568-1, 2026-10-16), the fix for CVE-2026-98432,
# which the Updates tab's ``KERNEL_FIX_DETAILS`` (frontend) explains to the
# user. Every machine the app probes boots Debian 13, so one floor covers them.
KERNEL_SECURITY_FLOOR_PACKAGE_VERSION: Final[str] = "6.12.113-1"
# The oldest template whose pinned apt snapshot resolves the fixed kernel
# package: a machine on an older template cannot install it until its template
# is updated, which moves the snapshot. That template is below the app's
# security floor, so the row already prompts for that update first.
OLDEST_KERNEL_FIXABLE_TEMPLATE_VERSION: Final[str] = "minds-v0.9.0"

# The probe reads a few files and dpkg's database.
KERNEL_PROBE_TIMEOUT_SECONDS: Final[float] = 60.0
# An apt install of a kernel package on a loaded machine.
KERNEL_INSTALL_TIMEOUT_SECONDS: Final[float] = 900.0
KERNEL_REBOOT_SCHEDULE_TIMEOUT_SECONDS: Final[float] = 60.0
# How long after the reboot is scheduled the transient timer fires it, so the
# exec that scheduled it returns before sshd goes down.
KERNEL_REBOOT_DELAY_SECONDS: Final[int] = 3

# Every verdict line the script prints starts with this; the rest is
# ``key=value`` tokens, free text carried base64-encoded.
_RESULT_MARKER: Final[str] = "MINDS_KERNEL"

_UPSTREAM_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)*)")


class KernelProbeTransport(UpperCaseStrEnum):
    """How the app reaches a workspace's machine as root."""

    AGENT = auto()
    """The agent's own host is the machine (a Lima VM, where the agent runs as root): a plain ``mngr exec``."""
    OUTER = auto()
    """The machine is the outer host of the agent's container (a cloud VPS): ``mngr exec --outer``."""


class KernelRestartMethod(UpperCaseStrEnum):
    """How a machine with the fixed kernel installed is brought to boot it."""

    HOST_CYCLE = auto()
    """The host stop and start are the machine's own halt and boot (a Lima VM; a cloud instance that AWS,
    GCP and Azure pause while its container is stopped), so the start boots the new kernel."""
    IN_PLACE_REBOOT = auto()
    """The host stop ends only the container and the machine keeps running (a Vultr VPS), so it is
    rebooted in place through the outer host between the stop and the start."""


class KernelMachineAccess(FrozenModel):
    """How the app reaches a workspace's machine as root, and how it restarts it."""

    transport: KernelProbeTransport = Field(description="The exec that runs the machine script")
    restart: KernelRestartMethod = Field(description="What boots the machine onto the installed kernel")


class KernelScriptStatus(UpperCaseStrEnum):
    """The verdict the in-machine script prints."""

    INSTALLED = auto()
    """The fixed kernel package is installed (already, or by this run); a restart activates it."""
    REBOOTING = auto()
    """The reboot is scheduled."""
    FAILED = auto()


class KernelProbe(FrozenModel):
    """What the in-machine script measured."""

    running_kernel: str = Field(description="``uname -r``")
    boot_id: str = Field(description="The kernel's boot id, which changes on every boot")
    metapackage: str = Field(description="The kernel metapackage for the machine's architecture")
    installed_package_version: str = Field(
        description="The installed version of the metapackage, '' when it is not installed"
    )


class KernelScriptAnswer(FrozenModel):
    """One run of the in-machine script: its probe line, and its verdict when it was asked for one."""

    probe: KernelProbe | None = Field(description="The measurements, when the script got that far")
    status: KernelScriptStatus | None = Field(description="The verdict; None for a bare probe")
    detail: str = Field(description="The verdict's free text")
    is_machine_reached: bool = Field(
        default=True,
        description="False when mngr answered with a failure in place of a result (a stopped host, a lost outer, an "
        "exec cut off on the way), which the restart answers by starting the machine and running the script again; "
        "True for a timed-out exec, which may still be running the script there, and for a result with no answer",
    )


# Runs as root on the machine: ``bash -s -- probe`` prints the probe line and
# stops; ``bash -s -- install <floor>`` installs the kernel metapackage when the
# installed one is below the floor; ``bash -s -- reboot`` schedules a reboot
# through a transient timer. Every exit prints one marker line. No ``set -e``:
# each step checks its own status.
_MACHINE_SCRIPT: Final[str] = r"""set -u
trap '' HUP PIPE
ACTION=$1
FLOOR=${2:-}
MARKER=MINDS_KERNEL
emit() { echo "$MARKER $*"; }
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
finish() {
    emit "status=$1 detail_b64=$(b64 "$2")"
    exit 0
}
is_at_least() { dpkg --compare-versions "$1" ge "$2"; }
arch=$(dpkg --print-architecture 2>/dev/null || echo unknown)
is_installed() { [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" = installed ]; }
# The flavour the machine already tracks: cloud (Lima, the cloud images) unless only the generic one is installed.
metapackage="linux-image-cloud-$arch"
if ! is_installed "$metapackage" && is_installed "linux-image-$arch"; then metapackage="linux-image-$arch"; fi
read_installed() { if is_installed "$metapackage"; then dpkg-query -W -f='${Version}' "$metapackage" 2>/dev/null; fi; }
running=$(uname -r)
boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
installed=$(read_installed)
emit "probe=1 running=$running boot_id=$boot_id metapackage=$metapackage installed=$installed"
case "$ACTION" in
probe)
    exit 0
    ;;
install)
    [ -n "$FLOOR" ] || finish failed "no floor was given"
    if [ -n "$installed" ] && is_at_least "$installed" "$FLOOR"; then
        finish installed "$metapackage $installed is already installed"
    fi
    export DEBIAN_FRONTEND=noninteractive
    output=$(apt-get update -qq 2>&1) || finish failed "apt-get update failed: $(printf '%s' "$output" | tail -c 400)"
    output=$(apt-get install -y -qq "$metapackage" 2>&1) \
        || finish failed "could not install $metapackage: $(printf '%s' "$output" | tail -c 400)"
    installed=$(read_installed)
    if [ -n "$installed" ] && is_at_least "$installed" "$FLOOR"; then
        finish installed "installed $metapackage $installed"
    fi
    finish failed "$metapackage is ${installed:-not installed} after the install, below $FLOOR"
    ;;
reboot)
    systemd-run --quiet --on-active=__REBOOT_DELAY__ --unit="minds-kernel-reboot-$$" systemctl reboot \
        || finish failed "could not schedule the reboot (systemd-run failed)"
    finish rebooting "reboot scheduled in __REBOOT_DELAY__s"
    ;;
*)
    finish failed "unknown action $ACTION"
    ;;
esac
""".replace("__REBOOT_DELAY__", str(KERNEL_REBOOT_DELAY_SECONDS))


@pure
def kernel_machine_access_for_provider(provider_name: str | None) -> KernelMachineAccess | None:
    """How a workspace on ``provider_name`` is reached and restarted, or None for a machine the app does not probe.

    Imbue Cloud slices are the fleet's to reboot, Modal sandboxes have no kernel
    of ours, and a local Docker container runs on the user's own machine, which
    is theirs to update.
    """
    if provider_name is None:
        return None
    match layout_for_provider_kind(provider_name):
        case HostAccessLayout.LIMA:
            return KernelMachineAccess(transport=KernelProbeTransport.AGENT, restart=KernelRestartMethod.HOST_CYCLE)
        case HostAccessLayout.VPS:
            # A Vultr VPS runs on through its container's stop; the connection backends pause the instance.
            restart = (
                KernelRestartMethod.IN_PLACE_REBOOT
                if provider_name == VULTR_PROVIDER_NAME
                else KernelRestartMethod.HOST_CYCLE
            )
            return KernelMachineAccess(transport=KernelProbeTransport.OUTER, restart=restart)
        case HostAccessLayout.IMBUE_CLOUD | HostAccessLayout.MODAL | HostAccessLayout.DOCKER | None:
            return None
        case _ as unreachable:
            raise AssertionError(f"unhandled host access layout {unreachable!r}")


@pure
def build_machine_command(action: str, floor_package_version: str) -> str:
    """The shell command running the machine script for ``action`` as the machine's root."""
    encoded_script = base64.b64encode(_MACHINE_SCRIPT.encode("utf-8")).decode("ascii")
    arguments = " ".join(shlex.quote(argument) for argument in (action, floor_package_version))
    return f"printf %s '{encoded_script}' | base64 -d | bash -s -- {arguments}"


@pure
def build_machine_exec_args(transport: KernelProbeTransport, workspace_address: str, command: str) -> list[str]:
    """The ``mngr`` argv that runs ``command`` as root on the workspace's machine, never starting a stopped one."""
    match transport:
        case KernelProbeTransport.AGENT:
            return ["exec", "--no-start", workspace_address, command, "--format", "json"]
        case KernelProbeTransport.OUTER:
            return [
                "exec",
                "--outer",
                "--missing-outer",
                "abort",
                "--no-start",
                workspace_address,
                command,
                "--format",
                "json",
            ]
        case _ as unreachable:
            raise AssertionError(f"unhandled transport {unreachable!r}")


@pure
def machine_stdout_from_exec_json(stdout: str) -> str | None:
    """The machine's own stdout from ``mngr exec --format json`` output, or None when the exec produced no result.

    An in-container exec answers under ``results`` and an ``--outer`` one under
    ``outer_results``; either way one row, since one agent was named.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning("The kernel exec's output was not the JSON envelope mngr emits: {}", e)
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("results", "outer_results"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return str(rows[0].get("stdout", ""))
    return None


def _decode_base64_text(encoded: str) -> str:
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError) as e:
        logger.debug("The machine script's detail was not base64; reading it as given: {}", e)
        return encoded


@pure
def parse_machine_script_output(stdout: str) -> KernelScriptAnswer:
    """Interpret the machine script's stdout; output without the marker reads as a failed, probe-less run."""
    probe: KernelProbe | None = None
    status: KernelScriptStatus | None = None
    detail = ""
    is_marker_seen = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_RESULT_MARKER + " "):
            continue
        is_marker_seen = True
        tokens = dict(token.split("=", 1) for token in stripped[len(_RESULT_MARKER) + 1 :].split() if "=" in token)
        if tokens.get("probe") == "1":
            probe = KernelProbe(
                running_kernel=tokens.get("running", ""),
                boot_id=tokens.get("boot_id", ""),
                metapackage=tokens.get("metapackage", ""),
                installed_package_version=tokens.get("installed", ""),
            )
            continue
        status_text = tokens.get("status", "").upper()
        if status_text not in {member.value for member in KernelScriptStatus}:
            status, detail = KernelScriptStatus.FAILED, f"unrecognized verdict {stripped!r}"
            continue
        status, detail = KernelScriptStatus(status_text), _decode_base64_text(tokens.get("detail_b64", ""))
    if not is_marker_seen:
        return KernelScriptAnswer(
            probe=None,
            status=KernelScriptStatus.FAILED,
            detail=f"the machine script produced no answer: {stdout[-300:]!r}",
        )
    return KernelScriptAnswer(probe=probe, status=status, detail=detail)


@pure
def upstream_kernel_version(text: str) -> tuple[int, ...]:
    """The dotted upstream version at the start of a kernel name or package version, as integers.

    ``6.12.113+deb13-cloud-amd64`` (uname -r) and ``6.12.113-1`` (the package)
    both read as ``(6, 12, 113)``; text with no leading version reads as empty.
    """
    match = _UPSTREAM_VERSION_RE.match(text)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


@pure
def is_running_kernel_at_least(running_kernel: str, floor_package_version: str) -> bool:
    """Whether ``uname -r`` names a kernel at or above the package version's upstream version."""
    running = upstream_kernel_version(running_kernel)
    return bool(running) and running >= upstream_kernel_version(floor_package_version)


@pure
def classify_kernel(probe: KernelProbe | None, floor_package_version: str) -> KernelSecurityStatus:
    """The machine's status against the floor; a probe that read no kernel is UNKNOWN, never vulnerable."""
    if probe is None or not upstream_kernel_version(probe.running_kernel):
        return KernelSecurityStatus.UNKNOWN
    if is_running_kernel_at_least(probe.running_kernel, floor_package_version):
        return KernelSecurityStatus.FIXED
    return KernelSecurityStatus.VULNERABLE


@pure
def is_fixed_kernel_installed(probe: KernelProbe, floor_package_version: str) -> bool:
    """Whether the installed metapackage is at or above the floor, so only a restart is missing."""
    installed = upstream_kernel_version(probe.installed_package_version)
    return bool(installed) and installed >= upstream_kernel_version(floor_package_version)


@pure
def is_kernel_fix_offered(
    status: KernelSecurityStatus,
    transport: KernelProbeTransport | None,
    probe: KernelProbe | None,
    workspace_version_ref: str,
    floor_package_version: str,
) -> bool:
    """Whether the app can move this machine onto the fixed kernel from here.

    A vulnerable machine the app can reach, whose template's apt snapshot resolves
    the fixed package, or that already has it installed and only needs the restart.
    """
    if status is not KernelSecurityStatus.VULNERABLE or transport is None or probe is None:
        return False
    if is_fixed_kernel_installed(probe, floor_package_version):
        return True
    workspace_version = parse_minds_version(workspace_version_ref)
    oldest_fixable = parse_minds_version(OLDEST_KERNEL_FIXABLE_TEMPLATE_VERSION)
    assert oldest_fixable is not None
    return workspace_version is not None and not workspace_version < oldest_fixable


@pure
def restarted_machine_shortfall(probe: KernelProbe, floor_package_version: str, previous_boot_id: str) -> str | None:
    """The row's sentence for a post-restart probe without a new boot on the fixed kernel; None once it has one."""
    if not probe.boot_id or probe.boot_id == previous_boot_id:
        return f"The machine has not restarted yet (boot id {probe.boot_id or 'unreadable'})."
    if not is_running_kernel_at_least(probe.running_kernel, floor_package_version):
        return f"The machine came back on {probe.running_kernel}, below {floor_package_version}."
    return None


def run_machine_script(
    mngr_caller: MngrCaller,
    transport: KernelProbeTransport,
    workspace_address: str,
    action: str,
    *,
    floor_package_version: str,
    timeout_seconds: float,
) -> KernelScriptAnswer:
    """Run the machine script's ``action`` as root on the workspace's machine and read its answer.

    An exec that gave no answer (a stopped host, a lost outer, a timeout) is a
    failed, probe-less answer in mngr's words rather than a raise, so a sweep
    reading many machines records it and goes on. Whether the machine was
    reached is read from mngr's exit alone: a cut-off exec cannot be told from
    an unreachable outer, since mngr reports both the same way.
    """
    argv = build_machine_exec_args(transport, workspace_address, build_machine_command(action, floor_package_version))
    result = mngr_caller.call(argv, timeout=timeout_seconds)
    machine_stdout = machine_stdout_from_exec_json(result.stdout) if result.returncode == 0 else None
    if result.is_timed_out or machine_stdout is None:
        reason = "timed out" if result.is_timed_out else (result.stderr.strip() or result.stdout.strip())[-300:]
        logger.debug("The kernel {} of {} did not answer: {}", action, workspace_address, reason)
        return KernelScriptAnswer(
            probe=None,
            status=KernelScriptStatus.FAILED,
            detail=f"could not reach the machine: {reason}",
            is_machine_reached=not (result.is_mngr_output and result.returncode != 0),
        )
    return parse_machine_script_output(machine_stdout)


def read_kernel_probe(
    mngr_caller: MngrCaller, transport: KernelProbeTransport, workspace_address: str
) -> KernelProbe | None:
    """The machine's kernel facts, or None when the probe did not answer."""
    answer = run_machine_script(
        mngr_caller,
        transport,
        workspace_address,
        "probe",
        floor_package_version=KERNEL_SECURITY_FLOOR_PACKAGE_VERSION,
        timeout_seconds=KERNEL_PROBE_TIMEOUT_SECONDS,
    )
    return answer.probe
