import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.errors import VpsConnectionError

# Idempotent install: skip if depot already on PATH, otherwise download to
# /usr/local/bin via depot.dev's official installer. Run once per build (cheap
# no-op when already present); avoids needing a separate provisioning step.
_DEPOT_INSTALL_CMD: Final[str] = "command -v depot >/dev/null 2>&1 || curl -fsSL https://depot.dev/install-cli.sh | sh"

# Env-var assignments whose values are secrets and must be redacted before any
# remote_command string ends up in logs or exception messages.
_SECRET_ENV_VARS: Final[tuple[str, ...]] = ("DEPOT_TOKEN",)
# Matches `VAR=value` where value is either a single-quoted string (with no
# embedded single quotes -- the form shlex.quote produces) or a run of
# non-whitespace characters. The leading word boundary prevents matching
# substrings like FOO_DEPOT_TOKEN=...
_SECRET_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(" + "|".join(_SECRET_ENV_VARS) + r")=(?:'[^']*'|\S+)")


def _redact_secret_env(remote_command: str) -> str:
    """Return remote_command with values of known-secret env-var assignments replaced.

    Used for log messages and exception messages so secrets like DEPOT_TOKEN
    never appear in trace logs or surface to the user.
    """
    return _SECRET_ENV_PATTERN.sub(r"\1=<redacted>", remote_command)


_SSH_BASE_OPTIONS: Final[tuple[str, ...]] = (
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
)


class DockerOverSsh(MutableModel):
    """Execute Docker commands on a remote VPS via SSH."""

    vps_ip: str = Field(frozen=True, description="IP address of the VPS")
    ssh_user: str = Field(frozen=True, default="root", description="SSH user on the VPS")
    ssh_key_path: Path = Field(frozen=True, description="Path to SSH private key for VPS")
    known_hosts_path: Path = Field(frozen=True, description="Path to known_hosts file for VPS")

    def _build_ssh_command(self, remote_command: str) -> list[str]:
        """Build the full SSH command to execute a remote command on the VPS."""
        return [
            "ssh",
            *_SSH_BASE_OPTIONS,
            "-i",
            str(self.ssh_key_path),
            "-o",
            f"UserKnownHostsFile={self.known_hosts_path}",
            f"{self.ssh_user}@{self.vps_ip}",
            remote_command,
        ]

    def run_ssh(self, remote_command: str, timeout_seconds: float = 60.0) -> str:
        """Run an arbitrary command on the VPS via SSH. Returns stdout."""
        cmd = self._build_ssh_command(remote_command)
        safe_command = _redact_secret_env(remote_command)
        logger.trace("SSH exec: {}", safe_command)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise VpsConnectionError(f"SSH command timed out after {timeout_seconds}s: {safe_command}") from e
        except OSError as e:
            raise VpsConnectionError(f"SSH command failed: {e}") from e
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            # Connection-level failures
            if "Connection refused" in error_msg or "No route to host" in error_msg:
                raise VpsConnectionError(f"Cannot reach VPS at {self.vps_ip}: {error_msg}")
            raise ContainerSetupError(f"Remote command failed (exit {result.returncode}): {error_msg}")
        return result.stdout

    def run_ssh_streaming(
        self,
        remote_command: str,
        on_output: Callable[[str], None],
        timeout_seconds: float = 600.0,
    ) -> None:
        """Run a command on the VPS via SSH, streaming stdout/stderr line by line.

        Each line is passed to on_output as it arrives. Raises ContainerSetupError
        if the command exits non-zero (with all captured output in the message).
        """
        cmd = self._build_ssh_command(remote_command)
        safe_command = _redact_secret_env(remote_command)
        logger.trace("SSH streaming: {}", safe_command)
        collected_output: list[str] = []
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as e:
            raise VpsConnectionError(f"SSH command failed: {e}") from e
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.rstrip("\n")
                collected_output.append(stripped)
                on_output(stripped)
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise VpsConnectionError(f"SSH command timed out after {timeout_seconds}s: {safe_command}") from None
        if returncode != 0:
            error_output = "\n".join(collected_output[-50:])
            raise ContainerSetupError(f"Remote command failed (exit {returncode}): {error_output}")

    def run_docker(self, docker_args: Sequence[str], timeout_seconds: float = 60.0) -> str:
        """Run a docker command on the VPS and return stdout."""
        remote_cmd = "docker " + " ".join(shlex.quote(a) for a in docker_args)
        return self.run_ssh(remote_cmd, timeout_seconds=timeout_seconds)

    def run_container(
        self,
        image: str,
        name: str,
        port_mappings: Mapping[str, str],
        volumes: Sequence[str],
        labels: Mapping[str, str],
        extra_args: Sequence[str],
        entrypoint_cmd: str,
    ) -> str:
        """Run a detached container. Returns container ID."""
        args: list[str] = ["run", "-d", "--name", name]
        for host_bind, container_port in port_mappings.items():
            args.extend(["-p", f"{host_bind}:{container_port}"])
        for vol in volumes:
            args.extend(["-v", vol])
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        args.extend(extra_args)
        args.extend(["--entrypoint", "sh", image, "-c", entrypoint_cmd])
        result = self.run_docker(args, timeout_seconds=120.0)
        container_id = result.strip()
        logger.debug("Started container {} ({})", name, container_id[:12])
        return container_id

    def stop_container(self, container_id_or_name: str, timeout_seconds: int = 10) -> None:
        """Stop a running container."""
        self.run_docker(["stop", "-t", str(timeout_seconds), container_id_or_name])

    def start_container(self, container_id_or_name: str) -> None:
        """Start a stopped container."""
        self.run_docker(["start", container_id_or_name])

    def remove_container(self, container_id_or_name: str, force: bool = False) -> None:
        """Remove a container."""
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(container_id_or_name)
        self.run_docker(args)

    def exec_in_container(self, container_id_or_name: str, command: str, timeout_seconds: float = 300.0) -> str:
        """Execute a command inside a running container."""
        return self.run_docker(
            ["exec", container_id_or_name, "sh", "-c", command],
            timeout_seconds=timeout_seconds,
        )

    def commit_container(self, container_id_or_name: str, image_name: str) -> str:
        """Commit a container as an image. Returns the image ID."""
        return self.run_docker(["commit", container_id_or_name, image_name]).strip()

    def inspect_container(self, container_id_or_name: str) -> dict:
        """Inspect a container and return parsed JSON."""
        output = self.run_docker(["inspect", container_id_or_name])
        data = json.loads(output)
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return data

    def container_is_running(self, container_id_or_name: str) -> bool:
        """Check if a container is running."""
        try:
            output = self.run_docker(["inspect", "--format", "{{.State.Running}}", container_id_or_name])
            return output.strip().lower() == "true"
        except ContainerSetupError as e:
            logger.debug("Container {} not running or not found: {}", container_id_or_name, e)
            return False

    def pull_image(self, image: str, timeout_seconds: float = 300.0) -> None:
        """Pull a Docker image on the VPS."""
        self.run_docker(["pull", image], timeout_seconds=timeout_seconds)

    def create_volume(self, name: str) -> None:
        """Create a Docker named volume on the VPS."""
        self.run_docker(["volume", "create", name])

    def remove_volume(self, name: str) -> None:
        """Remove a Docker named volume on the VPS."""
        self.run_docker(["volume", "rm", "-f", name])

    def volume_exists(self, name: str) -> bool:
        """Check if a Docker named volume exists on the VPS."""
        try:
            self.run_docker(["volume", "inspect", name])
            return True
        except ContainerSetupError as e:
            logger.debug("Volume {} not found: {}", name, e)
            return False

    def check_docker_ready(self) -> bool:
        """Check if Docker is installed and running on the VPS."""
        try:
            self.run_ssh("docker info > /dev/null 2>&1", timeout_seconds=15.0)
            return True
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.debug("Docker not ready on VPS {}: {}", self.vps_ip, e)
            return False

    def upload_directory(self, local_path: Path, remote_path: str, timeout_seconds: float = 300.0) -> None:
        """Upload a local directory to the VPS via rsync over SSH."""
        ssh_cmd = (
            f"ssh -i {shlex.quote(str(self.ssh_key_path))} "
            f"-o UserKnownHostsFile={shlex.quote(str(self.known_hosts_path))} "
            f"-o StrictHostKeyChecking=yes "
            f"-o BatchMode=yes "
            f"-o ConnectTimeout=15"
        )
        local_str = str(local_path).rstrip("/") + "/"
        cmd = [
            "rsync",
            "-az",
            "--delete",
            "--exclude=__pycache__",
            "--exclude=.venv",
            "--exclude=node_modules",
            "--exclude=.mypy_cache",
            "--exclude=.ruff_cache",
            "--exclude=.pytest_cache",
            "--exclude=.test_output",
            "--exclude=htmlcov",
            "--exclude=.test_durations",
            "-e",
            ssh_cmd,
            local_str,
            f"{self.ssh_user}@{self.vps_ip}:{remote_path}/",
        ]
        logger.debug("Uploading {} to VPS {}:{}", local_path, self.vps_ip, remote_path)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as e:
            raise VpsConnectionError(f"Upload timed out after {timeout_seconds}s") from e
        if result.returncode != 0:
            raise ContainerSetupError(f"Upload failed: {result.stderr.strip()}")

    def build_image(
        self,
        tag: str,
        build_context_path: str,
        docker_build_args: Sequence[str],
        timeout_seconds: float = 600.0,
        on_output: Callable[[str], None] | None = None,
    ) -> str:
        """Build a Docker image on the VPS via depot.dev. Returns the image tag.

        Ensures the depot CLI is installed on the VPS (idempotent), forwards
        DEPOT_TOKEN (required) and DEPOT_PROJECT_ID (optional, when set) from
        the agent's environment, and runs `depot build --load` so the result
        is imported into the local Docker daemon on the VPS for subsequent
        `docker run`.
        """
        depot_token = os.environ.get("DEPOT_TOKEN", "")
        depot_project_id = os.environ.get("DEPOT_PROJECT_ID", "")
        if not depot_token:
            raise ContainerSetupError(
                "VPS image builds require DEPOT_TOKEN in the agent's environment. "
                "Set DEPOT_TOKEN (and DEPOT_PROJECT_ID if no depot.json is on the VPS)."
            )
        args = ["build", "--load", "-t", tag] + list(docker_build_args) + [build_context_path]
        quoted = " ".join(shlex.quote(a) for a in args)
        env_prefix_parts = [f"DEPOT_TOKEN={shlex.quote(depot_token)}"]
        if depot_project_id:
            env_prefix_parts.append(f"DEPOT_PROJECT_ID={shlex.quote(depot_project_id)}")
        env_prefix = " ".join(env_prefix_parts)
        remote_cmd = f"{_DEPOT_INSTALL_CMD} && {env_prefix} depot {quoted}"
        if on_output is not None:
            self.run_ssh_streaming(remote_cmd, on_output=on_output, timeout_seconds=timeout_seconds)
        else:
            self.run_ssh(remote_cmd, timeout_seconds=timeout_seconds)
        return tag

    def check_file_exists(self, path: str) -> bool:
        """Check if a file exists on the VPS."""
        try:
            self.run_ssh(f"test -f {shlex.quote(path)}", timeout_seconds=10.0)
            return True
        except ContainerSetupError as e:
            logger.debug("File {} not found on VPS {}: {}", path, self.vps_ip, e)
            return False
