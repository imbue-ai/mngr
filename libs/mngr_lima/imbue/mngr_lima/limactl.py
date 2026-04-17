import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.constants import MINIMUM_LIMA_VERSION
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.errors import LimaNotInstalledError
from imbue.mngr_lima.errors import LimaVersionError


def _log_lima_output(line: str, is_stdout: bool) -> None:
    """Log output from limactl commands at BUILD level."""
    line = line.strip()
    if line:
        logger.log(LogLevel.BUILD.value, "{}", line, source="lima")


class _SerialLogTailerCallback(MutableModel):
    """Output callback that logs limactl output and tails the VM serial log.

    When limactl prints a line mentioning the serial log path, this
    starts a background ``tail -f`` on that file so boot progress
    is visible alongside limactl output.
    """

    model_config = {"arbitrary_types_allowed": True}

    cg: ConcurrencyGroup = Field(frozen=True, description="Concurrency group for the tail process")
    tailer_started: bool = Field(default=False, description="Whether the serial log tailer has been started")

    def __call__(self, line: str, is_stdout: bool) -> None:
        stripped = line.strip()
        if not stripped:
            return
        logger.log(LogLevel.BUILD.value, "{}", stripped, source="lima")

        if not self.tailer_started and "serial" in stripped and ".log" in stripped:
            # Match an absolute path containing "serial" and ending with ".log".
            # limactl escapes quotes in its log output, so we match the path
            # directly rather than relying on surrounding quote characters.
            match = re.search(r"(/\S+serial\S*\.log)", stripped)
            if match:
                log_pattern = match.group(1)
                serial_log = re.sub(r"\*", "", log_pattern)
                self.tailer_started = True
                _start_serial_tailer(self.cg, serial_log)


def _log_boot_output(line: str, is_stdout: bool) -> None:
    """Log serial/boot output at BUILD level with boot source tag."""
    stripped = line.strip()
    if stripped:
        logger.log(LogLevel.BUILD.value, "{}", stripped, source="boot")


_active_serial_tailer: RunningProcess | None = None


def _start_serial_tailer(cg: ConcurrencyGroup, serial_log_path: str) -> None:
    """Start tailing a serial log file in the background.

    Uses ``run_process_in_background`` with ``is_checked_by_group=False``
    so the ConcurrencyGroup won't wait for it on exit. The process is
    terminated explicitly via ``_stop_serial_tailer``.
    """
    global _active_serial_tailer
    _active_serial_tailer = cg.run_process_in_background(
        ["tail", "--follow=name", "--retry", serial_log_path],
        is_checked_by_group=False,
        on_output=_log_boot_output,
    )


def _stop_serial_tailer() -> None:
    """Kill the serial log tailer process if running."""
    global _active_serial_tailer
    if _active_serial_tailer is not None:
        _active_serial_tailer.terminate(force_kill_seconds=2.0)
        _active_serial_tailer = None


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


def _normalize_start_args(start_args: tuple[str, ...]) -> list[str]:
    """Normalize start args for limactl.

    limactl expects --memory and --disk as plain numbers (GiB), but users and
    templates may pass human-readable values like ``--memory=4GiB`` or
    ``--disk=100GiB``.  Strip the unit suffix so limactl can parse them.
    """
    result: list[str] = []
    for arg in start_args:
        for flag in ("--memory=", "--disk="):
            if arg.startswith(flag):
                value = arg[len(flag):]
                for suffix in ("TiB", "tib", "TB", "tb", "GiB", "gib", "GB", "gb", "MiB", "mib", "MB", "mb"):
                    if value.endswith(suffix):
                        value = value[: -len(suffix)]
                        break
                arg = f"{flag}{value}"
                break
        result.append(arg)
    return result


def limactl_start_new(
    cg: ConcurrencyGroup,
    instance_name: str,
    yaml_path: Path,
    start_args: tuple[str, ...] = (),
    timeout: float = 1800.0,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Create and start a new Lima instance from a YAML config file.

    Runs: limactl start --name=<instance_name> <yaml_path> [start_args...]
    Output is streamed via on_output (defaults to BUILD-level logging).
    """
    normalized_args = _normalize_start_args(start_args)
    cmd = ["limactl", "--log-level=info", "start", f"--name={instance_name}", str(yaml_path)] + normalized_args
    effective_callback = on_output or _SerialLogTailerCallback(cg=cg)
    try:
        with log_span("Running limactl start for new instance: {}", instance_name):
            result = cg.run_process_to_completion(
                cmd,
                timeout=timeout,
                on_output=effective_callback,
                is_checked_after=False,
            )
    finally:
        _stop_serial_tailer()
    if result.returncode != 0:
        raise LimaCommandError("start", result.returncode, result.stderr)


def limactl_start_existing(
    cg: ConcurrencyGroup,
    instance_name: str,
    timeout: float = 300.0,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Start an existing stopped Lima instance.

    Runs: limactl start <instance_name>
    """
    cmd = ["limactl", "--log-level=info", "start", instance_name]
    with log_span("Running limactl start for existing instance: {}", instance_name):
        result = cg.run_process_to_completion(
            cmd,
            timeout=timeout,
            on_output=on_output or _log_lima_output,
            is_checked_after=False,
        )
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
