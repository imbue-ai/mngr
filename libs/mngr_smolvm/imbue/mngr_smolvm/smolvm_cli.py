import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.constants import MINIMUM_SMOLVM_VERSION
from imbue.mngr_smolvm.errors import SmolvmCapabilityError
from imbue.mngr_smolvm.errors import SmolvmCommandError
from imbue.mngr_smolvm.errors import SmolvmNotInstalledError
from imbue.mngr_smolvm.errors import SmolvmVersionError


def _log_smolvm_output(line: str, is_stdout: bool) -> None:
    """Log output from smolvm commands at BUILD level."""
    line = line.strip()
    if line:
        logger.log(LogLevel.BUILD.value, "{}", line, source="smolvm")


def check_smolvm_installed(provider_name: ProviderInstanceName, smolvm_command: str) -> None:
    """Verify that the smolvm binary is reachable. Raises SmolvmNotInstalledError if not."""
    if shutil.which(smolvm_command) is None:
        raise SmolvmNotInstalledError(provider_name, smolvm_command)


def get_smolvm_version(cg: ConcurrencyGroup, smolvm_command: str) -> tuple[int, int, int]:
    """Get the installed smolvm version as (major, minor, patch) from `smolvm --version`."""
    result = cg.run_process_to_completion([smolvm_command, "--version"], timeout=10.0)
    version_str = result.stdout.strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if match is None:
        raise SmolvmCommandError("--version", result.returncode, f"Could not parse version from: {version_str}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def check_smolvm_version(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    smolvm_command: str,
    minimum: tuple[int, int, int] = MINIMUM_SMOLVM_VERSION,
) -> None:
    """Verify smolvm meets the minimum version requirement."""
    installed = get_smolvm_version(cg, smolvm_command)
    if installed < minimum:
        installed_str = ".".join(str(v) for v in installed)
        minimum_str = ".".join(str(v) for v in minimum)
        raise SmolvmVersionError(provider_name, installed_str, minimum_str)


def check_smolvm_data_disk_support(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    smolvm_command: str,
) -> None:
    """Verify the installed smolvm build supports persistent data disks.

    Probes `smolvm machine create --help` for the --data-disk flag, which
    only builds with the btrfs-capable guest kernel and agent expose. The
    default virtiofs-exposed layout works on stock smolvm; only the btrfs
    layout needs this capability.
    """
    result = cg.run_process_to_completion([smolvm_command, "machine", "create", "--help"], timeout=10.0)
    if result.returncode != 0:
        raise SmolvmCommandError("machine create --help", result.returncode, result.stderr)
    if "--data-disk" not in result.stdout:
        raise SmolvmCapabilityError(provider_name, "persistent data disks (--data-disk)")


def smolvm_machine_name(host_name: HostName, prefix: str) -> str:
    """Build the smolvm machine name from a mngr host name.

    The prefix is the mngr config prefix (default 'mngr-').
    """
    return f"{prefix}{host_name}"


def smolvm_machine_create(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    machine_name: str,
    cpus: int,
    memory_mib: int,
    # OCI image reference to run as the workload (None for bare VM mode).
    image: str | None,
    # Path to a .smolmachine sidecar to create the machine from.
    from_pack: Path | None,
    # (host_port, guest_port) TCP forwards.
    ports: tuple[tuple[int, int], ...],
    # (host_path, guest_path) virtiofs mounts.
    volumes: tuple[tuple[str, str], ...],
    # --data-disk spec string, e.g. "size=100,target=/mngr" (None for no data disk).
    data_disk: str | None,
    extra_args: tuple[str, ...],
    timeout: float = 120.0,
) -> None:
    """Create a smolvm machine: smolvm machine create --name <name> --net ..."""
    cmd = [smolvm_command, "machine", "create", "--name", machine_name, "--net"]
    cmd.extend(["--cpus", str(cpus), "--mem", str(memory_mib)])
    if image is not None:
        cmd.extend(["--image", image])
    if from_pack is not None:
        cmd.extend(["--from", str(from_pack)])
    for host_port, guest_port in ports:
        cmd.extend(["--port", f"{host_port}:{guest_port}"])
    for host_path, guest_path in volumes:
        cmd.extend(["--volume", f"{host_path}:{guest_path}"])
    if data_disk is not None:
        cmd.extend(["--data-disk", data_disk])
    cmd.extend(extra_args)
    with log_span("Running smolvm machine create: {}", machine_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout, on_output=_log_smolvm_output)
    if result.returncode != 0:
        raise SmolvmCommandError("machine create", result.returncode, result.stderr)


def smolvm_machine_start(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    machine_name: str,
    timeout: float = 120.0,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Start a smolvm machine: smolvm machine start --name <name>."""
    cmd = [smolvm_command, "machine", "start", "--name", machine_name]
    with log_span("Running smolvm machine start: {}", machine_name):
        result = cg.run_process_to_completion(
            cmd,
            timeout=timeout,
            on_output=on_output or _log_smolvm_output,
        )
    if result.returncode != 0:
        raise SmolvmCommandError("machine start", result.returncode, result.stderr)


def smolvm_machine_stop(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    machine_name: str,
    timeout: float = 120.0,
) -> None:
    """Stop a running smolvm machine: smolvm machine stop --name <name>."""
    cmd = [smolvm_command, "machine", "stop", "--name", machine_name]
    with log_span("Running smolvm machine stop: {}", machine_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise SmolvmCommandError("machine stop", result.returncode, result.stderr)


def smolvm_machine_delete(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    machine_name: str,
    timeout: float = 120.0,
) -> None:
    """Delete a smolvm machine and its data: smolvm machine rm --name <name> --force.

    Tolerates the machine already being absent.
    """
    cmd = [smolvm_command, "machine", "rm", "--name", machine_name, "--force"]
    with log_span("Running smolvm machine rm: {}", machine_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "not found" in stderr_lower or "does not exist" in stderr_lower:
            logger.debug("smolvm machine {} already absent, skipping", machine_name)
            return
        raise SmolvmCommandError("machine rm", result.returncode, result.stderr)


def smolvm_machine_list(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """List all smolvm machines as parsed JSON: smolvm machine ls --json."""
    cmd = [smolvm_command, "machine", "ls", "--json"]
    result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise SmolvmCommandError("machine ls", result.returncode, result.stderr)
    output = result.stdout.strip()
    if not output:
        return []
    try:
        machines = json.loads(output)
    except json.JSONDecodeError as e:
        raise SmolvmCommandError("machine ls", result.returncode, f"invalid JSON output: {e}") from e
    if not isinstance(machines, list):
        raise SmolvmCommandError("machine ls", result.returncode, f"expected JSON list, got {type(machines)}")
    return machines


def smolvm_machine_exec(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    machine_name: str,
    command: str,
    timeout: float = 120.0,
) -> tuple[int | None, str, str]:
    """Execute a shell command inside a smolvm machine via the vsock channel.

    Runs: smolvm machine exec --name <name> -- sh -c <command>
    Returns: (returncode, stdout, stderr)

    This works before (and without) sshd, so it is the bootstrap channel
    used to provision SSH access.
    """
    cmd = [smolvm_command, "machine", "exec", "--name", machine_name, "--", "sh", "-c", command]
    result = cg.run_process_to_completion(cmd, timeout=timeout, is_checked_after=False)
    return result.returncode, result.stdout, result.stderr


def smolvm_pack_create_from_archive(
    cg: ConcurrencyGroup,
    smolvm_command: str,
    archive_path: Path,
    output_path: Path,
    timeout: float = 600.0,
) -> None:
    """Convert a docker-save image archive into a .smolmachine pack.

    Runs: smolvm pack create --from-archive <archive> -o <output>
    """
    cmd = [
        smolvm_command,
        "pack",
        "create",
        "--from-archive",
        str(archive_path),
        "-o",
        str(output_path),
    ]
    with log_span("Running smolvm pack create --from-archive: {}", archive_path):
        result = cg.run_process_to_completion(cmd, timeout=timeout, on_output=_log_smolvm_output)
    if result.returncode != 0:
        raise SmolvmCommandError("pack create --from-archive", result.returncode, result.stderr)
