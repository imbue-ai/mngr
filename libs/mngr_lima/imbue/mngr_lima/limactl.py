import json
import re
import shutil
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.constants import MINIMUM_LIMA_VERSION
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.errors import LimaNotInstalledError
from imbue.mngr_lima.errors import LimaVersionError


def check_lima_installed(provider_name: ProviderInstanceName) -> None:
    """Verify that limactl is on PATH. Raises LimaNotInstalledError if not."""
    if shutil.which("limactl") is None:
        raise LimaNotInstalledError(provider_name)


def get_lima_version(cg: ConcurrencyGroup) -> tuple[int, int, int]:
    """Get the installed Lima version as (major, minor, patch).

    Parses the output of `limactl --version`.
    """
    result = cg.run_process_to_completion(["limactl", "--version"], timeout=10.0)
    version_str = result.stdout.strip()
    # limactl --version outputs something like "limactl version 1.0.2"
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if match is None:
        raise LimaCommandError("--version", 0, f"Could not parse version from: {version_str}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def check_lima_version(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    minimum: tuple[int, int, int] = MINIMUM_LIMA_VERSION,
) -> None:
    """Verify Lima meets the minimum version requirement."""
    installed = get_lima_version(cg)
    if installed < minimum:
        installed_str = ".".join(str(v) for v in installed)
        minimum_str = ".".join(str(v) for v in minimum)
        raise LimaVersionError(provider_name, installed_str, minimum_str)


def lima_instance_name(host_name: HostName, prefix: str) -> str:
    """Build the Lima instance name from a mngr host name.

    The prefix is the mngr config prefix (default 'mngr-').
    """
    return f"{prefix}{host_name}"


def host_name_from_instance_name(instance_name: str, prefix: str) -> HostName | None:
    """Extract the mngr host name from a Lima instance name.

    Returns None if the instance name does not start with the prefix.
    """
    if not instance_name.startswith(prefix):
        return None
    name = instance_name[len(prefix) :]
    if not name:
        return None
    return HostName(name)


def limactl_start_new(
    cg: ConcurrencyGroup,
    instance_name: str,
    yaml_path: Path,
    start_args: tuple[str, ...] = (),
    timeout: float = 600.0,
) -> None:
    """Create and start a new Lima instance from a YAML config file.

    Runs: limactl start --name=<instance_name> <yaml_path> [start_args...]
    """
    cmd = ["limactl", "start", f"--name={instance_name}", str(yaml_path)] + list(start_args)
    with log_span("Running limactl start for new instance: {}", instance_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("start", result.returncode, result.stderr)


def limactl_start_existing(
    cg: ConcurrencyGroup,
    instance_name: str,
    timeout: float = 300.0,
) -> None:
    """Start an existing stopped Lima instance.

    Runs: limactl start <instance_name>
    """
    cmd = ["limactl", "start", instance_name]
    with log_span("Running limactl start for existing instance: {}", instance_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("start", result.returncode, result.stderr)


def limactl_stop(
    cg: ConcurrencyGroup,
    instance_name: str,
    timeout: float = 120.0,
) -> None:
    """Stop a running Lima instance.

    Runs: limactl stop <instance_name>
    """
    cmd = ["limactl", "stop", instance_name]
    with log_span("Running limactl stop: {}", instance_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("stop", result.returncode, result.stderr)


def limactl_delete(
    cg: ConcurrencyGroup,
    instance_name: str,
    force: bool = True,
    timeout: float = 60.0,
) -> None:
    """Delete a Lima instance.

    Runs: limactl delete [--force] <instance_name>
    """
    cmd = ["limactl", "delete"]
    if force:
        cmd.append("--force")
    cmd.append(instance_name)
    with log_span("Running limactl delete: {}", instance_name):
        result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("delete", result.returncode, result.stderr)


def limactl_list(cg: ConcurrencyGroup, timeout: float = 30.0) -> list[dict[str, Any]]:
    """List all Lima instances as parsed JSON.

    Runs: limactl list --json
    """
    cmd = ["limactl", "list", "--json"]
    result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("list", result.returncode, result.stderr)

    output = result.stdout.strip()
    if not output:
        return []

    # limactl list --json outputs one JSON object per line (JSONL format)
    instances: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            try:
                instances.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse Lima instance JSON: {}", e)
    return instances


class LimaSshConfig(FrozenModel):
    """Parsed SSH connection info from limactl show-ssh."""

    hostname: str = Field(description="SSH hostname (usually 127.0.0.1)")
    port: int = Field(description="SSH port number")
    user: str = Field(description="SSH username")
    identity_file: Path = Field(description="Path to SSH identity file")


def _strip_ssh_config_quotes(value: str) -> str:
    """Strip surrounding double quotes from an SSH config value.

    SSH config format (used by limactl show-ssh --format config) wraps
    values like IdentityFile in double quotes, e.g. IdentityFile "/path/to/key".
    """
    return value.strip().strip('"').strip()


def limactl_show_ssh(
    cg: ConcurrencyGroup,
    instance_name: str,
    timeout: float = 10.0,
) -> LimaSshConfig:
    """Get SSH connection info for a Lima instance.

    Parses the output of: limactl show-ssh --format config <instance_name>
    """
    cmd = ["limactl", "show-ssh", "--format", "config", instance_name]
    result = cg.run_process_to_completion(cmd, timeout=timeout)
    if result.returncode != 0:
        raise LimaCommandError("show-ssh", result.returncode, result.stderr)

    hostname = "127.0.0.1"
    port = 22
    user = "root"
    identity_file = Path.home() / ".lima" / "_config" / "user"

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("HostName "):
            hostname = _strip_ssh_config_quotes(line.split(None, 1)[1])
        elif line.startswith("Port "):
            port = int(_strip_ssh_config_quotes(line.split(None, 1)[1]))
        elif line.startswith("User "):
            user = _strip_ssh_config_quotes(line.split(None, 1)[1])
        elif line.startswith("IdentityFile "):
            identity_file = Path(_strip_ssh_config_quotes(line.split(None, 1)[1]))

    return LimaSshConfig(hostname=hostname, port=port, user=user, identity_file=identity_file)


def limactl_shell(
    cg: ConcurrencyGroup,
    instance_name: str,
    command: str,
    timeout: float = 60.0,
) -> tuple[int | None, str, str]:
    """Execute a command inside a Lima instance.

    Runs: limactl shell <instance_name> -- sh -c <command>
    Returns: (returncode, stdout, stderr)
    """
    cmd = ["limactl", "shell", instance_name, "--", "sh", "-c", command]
    result = cg.run_process_to_completion(cmd, timeout=timeout)
    return result.returncode, result.stdout, result.stderr
