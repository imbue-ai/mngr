import json
import shlex
from collections.abc import Mapping
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostConfig
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# State container configuration
STATE_CONTAINER_IMAGE: Final[str] = "alpine:latest"
_FILE_SEP: Final[str] = "---MNGR_FILE_SEP---"
STATE_VOLUME_MOUNT_PATH: Final[str] = "/mngr-state"
CONTAINER_ENTRYPOINT_CMD: Final[str] = "trap 'exit 0' TERM; tail -f /dev/null & wait"


class VpsHostConfig(HostConfig):
    """VPS-specific host configuration stored in the host record."""

    vps_instance_id: VpsInstanceId = Field(description="Provider-specific VPS instance ID")
    region: str = Field(description="Region where the VPS was created")
    plan: str = Field(description="VPS plan (CPU/RAM specification)")
    os_id: int = Field(description="OS image ID used to create the VPS")
    start_args: tuple[str, ...] = Field(default=(), description="Docker run arguments for replay on snapshot restore")
    image: str | None = Field(default=None, description="Docker image used for the container")
    container_name: str = Field(description="Docker container name on the VPS")
    volume_name: str = Field(description="Docker volume name on the VPS")
    vps_ssh_key_id: str | None = Field(default=None, description="Provider SSH key ID (for cleanup on destroy)")


class VpsDockerHostRecord(FrozenModel):
    """Host metadata stored on the VPS state volume."""

    certified_host_data: CertifiedHostData = Field(frozen=True, description="The certified host data")
    vps_ip: str | None = Field(default=None, description="Current IP address of the VPS")
    ssh_host_public_key: str | None = Field(default=None, description="VPS SSH host public key")
    container_ssh_host_public_key: str | None = Field(default=None, description="Container SSH host public key")
    config: VpsHostConfig | None = Field(default=None, description="VPS and container configuration")
    container_id: str | None = Field(default=None, description="Docker container ID")


def _run_outer_command(outer: OuterHostInterface, command: str, *, label: str) -> str:
    """Run a command on the outer host; raise MngrError on non-zero exit."""
    result = outer.execute_idempotent_command(command)
    if not result.success:
        raise MngrError(
            f"VPS outer command {label!r} failed: stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return result.stdout


def _container_is_running(outer: OuterHostInterface, container_name: str) -> bool:
    """Check whether the named docker container is running on the outer."""
    result = outer.execute_idempotent_command(
        f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(container_name)}"
    )
    if not result.success:
        return False
    return result.stdout.strip().lower() == "true"


def ensure_state_container(
    outer: OuterHostInterface,
    prefix: str,
    user_id: str,
    provider_name: str,
) -> str:
    """Ensure the singleton state container exists and is running on the outer host.

    Creates a Docker named volume and a small Alpine container that mounts it.
    Returns the container name.
    """
    container_name = f"{prefix}docker-state-{user_id}"
    volume_name = f"{prefix}docker-state-{user_id}"

    logger.info("ensure_state_container: checking for {} on outer {}", container_name, outer.get_name())

    # List all containers on the VPS for diagnostics
    diag = outer.execute_idempotent_command("docker ps -a --format '{{.Names}} {{.Status}} {{.ID}}'")
    if diag.success:
        logger.info("ensure_state_container: existing containers on outer:\n{}", diag.stdout.strip() or "(none)")
    else:
        logger.warning("ensure_state_container: failed to list containers: {}", diag.stderr.strip())

    # Check if already running
    if _container_is_running(outer, container_name):
        logger.info("ensure_state_container: {} is already running", container_name)
        return container_name

    # Try to start if it exists but is stopped
    start_result = outer.execute_idempotent_command(f"docker start {shlex.quote(container_name)}")
    if start_result.success:
        logger.info("ensure_state_container: started existing stopped container {}", container_name)
        return container_name
    logger.info(
        "ensure_state_container: {} does not exist yet, will create: {}",
        container_name,
        start_result.stderr.strip(),
    )

    # Create the volume and container
    logger.info(
        "ensure_state_container: creating volume {} and container {} on outer {}",
        volume_name,
        container_name,
        outer.get_name(),
    )
    _run_outer_command(outer, f"docker volume create {shlex.quote(volume_name)}", label="docker-volume-create")

    run_args = [
        "docker",
        "run",
        "-d",
        "--name",
        shlex.quote(container_name),
        "-v",
        shlex.quote(f"{volume_name}:{STATE_VOLUME_MOUNT_PATH}:rw"),
        "--label",
        shlex.quote(f"com.imbue.mngr.provider={provider_name}"),
        "--label",
        "com.imbue.mngr.type=state-container",
        "--restart",
        "unless-stopped",
        "--entrypoint",
        "sh",
        STATE_CONTAINER_IMAGE,
        "-c",
        shlex.quote(CONTAINER_ENTRYPOINT_CMD),
    ]
    _run_outer_command(outer, " ".join(run_args), label="docker-run-state")
    logger.info("ensure_state_container: successfully created {}", container_name)
    return container_name


class VpsDockerHostStore:
    """Host record store backed by a state container on the VPS.

    Mirrors DockerHostStore but operates over an OuterHostInterface (so all
    docker commands run via SSH on the VPS root account).
    """

    def __init__(self, outer: OuterHostInterface, state_container_name: str) -> None:
        self._outer = outer
        self._state_container_name = state_container_name
        self._cache: dict[HostId, VpsDockerHostRecord] = {}

    def _host_record_path(self, host_id: HostId) -> str:
        return f"{STATE_VOLUME_MOUNT_PATH}/host_state/{host_id}.json"

    def _agent_data_dir(self, host_id: HostId) -> str:
        return f"{STATE_VOLUME_MOUNT_PATH}/host_state/{host_id}"

    def _agent_data_path(self, host_id: HostId, agent_id: AgentId) -> str:
        return f"{STATE_VOLUME_MOUNT_PATH}/host_state/{host_id}/{agent_id}.json"

    def _exec_in_state_container(self, command: str) -> str:
        """Run a shell command inside the state container via the outer host."""
        full_command = f"docker exec {shlex.quote(self._state_container_name)} sh -c {shlex.quote(command)}"
        return _run_outer_command(self._outer, full_command, label="state-container-exec")

    def write_host_record(self, host_record: VpsDockerHostRecord) -> None:
        """Write a host record to the state volume."""
        host_id = HostId(host_record.certified_host_data.host_id)
        path = self._host_record_path(host_id)
        data = host_record.model_dump_json(indent=2)
        # Ensure parent directory exists and write atomically
        parent_dir = path.rsplit("/", 1)[0]
        self._exec_in_state_container(f"mkdir -p '{parent_dir}' && cat > '{path}' << 'MNGR_EOF'\n{data}\nMNGR_EOF")
        logger.trace("Wrote host record: {}", path)
        self._cache[host_id] = host_record

    def read_host_record(self, host_id: HostId, is_cache_enabled: bool = True) -> VpsDockerHostRecord | None:
        """Read a host record from the state volume. Returns None if not found."""
        if is_cache_enabled and host_id in self._cache:
            return self._cache[host_id]

        path = self._host_record_path(host_id)
        try:
            data = self._exec_in_state_container(f"cat '{path}'")
            host_record = VpsDockerHostRecord.model_validate_json(data)
            self._cache[host_id] = host_record
            return host_record
        except MngrError as e:
            logger.debug("Host record {} not found on state volume: {}", host_id, e)
            return None
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse host record {}: {}", path, e)
            return None

    def delete_host_record(self, host_id: HostId) -> None:
        """Delete a host record and associated agent data."""
        agent_dir = self._agent_data_dir(host_id)
        try:
            self._exec_in_state_container(f"rm -rf '{agent_dir}'")
        except MngrError as e:
            logger.trace("No agent data to clean up for {}: {}", host_id, e)

        path = self._host_record_path(host_id)
        try:
            self._exec_in_state_container(f"rm -f '{path}'")
        except MngrError as e:
            logger.warning("Failed to delete host record {}: {}", host_id, e)

        self._cache.pop(host_id, None)

    def list_all_host_records(self) -> list[VpsDockerHostRecord]:
        """List all host records stored on the state volume in a single SSH command."""
        state_dir = f"{STATE_VOLUME_MOUNT_PATH}/host_state"
        script = (
            f'for f in \'{state_dir}\'/*.json; do [ -f "$f" ] || continue; echo \'{_FILE_SEP}\'"$f"; cat "$f"; done'
        )
        try:
            output = self._exec_in_state_container(script)
        except MngrError as e:
            logger.debug("No host records found on state volume: {}", e)
            return []

        return self._parse_batched_host_records(output)

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Write agent data for offline listing."""
        agent_id_value = agent_data.get("id")
        if not agent_id_value:
            logger.warning("Cannot persist agent data without id field")
            return

        path = self._agent_data_path(host_id, AgentId(str(agent_id_value)))
        data = json.dumps(dict(agent_data), indent=2)
        parent_dir = path.rsplit("/", 1)[0]
        self._exec_in_state_container(f"mkdir -p '{parent_dir}' && cat > '{path}' << 'MNGR_EOF'\n{data}\nMNGR_EOF")
        logger.trace("Persisted agent data: {}", path)

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        """Read persisted agent data for a host in a single SSH command."""
        agent_dir = self._agent_data_dir(host_id)
        script = (
            f'for f in \'{agent_dir}\'/*.json; do [ -f "$f" ] || continue; echo \'{_FILE_SEP}\'"$f"; cat "$f"; done'
        )
        try:
            output = self._exec_in_state_container(script)
        except MngrError as e:
            logger.debug("No agent data found for host {}: {}", host_id, e)
            return []

        return self._parse_batched_json_files(output)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove persisted agent data."""
        path = self._agent_data_path(host_id, agent_id)
        try:
            self._exec_in_state_container(f"rm -f '{path}'")
        except MngrError as e:
            logger.warning("Failed to remove agent data {}: {}", path, e)

    def list_all_host_records_with_agents(
        self,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Read all host records and their agent data in a single SSH command.

        Returns (host_records, agent_data_by_host_id).
        """
        state_dir = f"{STATE_VOLUME_MOUNT_PATH}/host_state"
        # Read all .json files at the top level (host records) and in subdirs (agent data)
        script = (
            f"for f in '{state_dir}'/*.json '{state_dir}'/*/*.json; do "
            f'[ -f "$f" ] || continue; '
            f"echo '{_FILE_SEP}'\"$f\"; "
            f'cat "$f"; '
            f"done"
        )
        try:
            output = self._exec_in_state_container(script)
        except MngrError as e:
            logger.debug("No records found on state volume: {}", e)
            return [], {}

        host_records = self._parse_batched_host_records(output)
        agent_data_by_host_id: dict[HostId, list[dict[str, Any]]] = {}
        # Parse agent data files (those in subdirectories like /host_state/<host-id>/<agent-id>.json)
        # Agent data lives in subdirectories: /host_state/<host-id>/<agent-id>.json
        for file_path, content in self._split_batched_output(output):
            relative = file_path.removeprefix(f"{state_dir}/")
            parts = relative.split("/")
            if len(parts) == 2 and parts[1].endswith(".json"):
                host_id = HostId(parts[0])
                try:
                    agent_data = json.loads(content)
                    agent_data_by_host_id.setdefault(host_id, []).append(agent_data)
                except json.JSONDecodeError as e:
                    logger.warning("Skipped invalid agent record {}: {}", file_path, e)

        return host_records, agent_data_by_host_id

    def _split_batched_output(self, output: str) -> list[tuple[str, str]]:
        """Split batched output into (file_path, content) pairs."""
        results: list[tuple[str, str]] = []
        if not output.strip():
            return results

        parts = output.split(_FILE_SEP)
        # Skip content before first separator
        for part in parts[1:]:
            lines = part.split("\n", 1)
            if len(lines) < 2:
                continue
            file_path = lines[0].strip()
            content = lines[1].strip()
            if file_path and content:
                results.append((file_path, content))
        return results

    def _parse_batched_host_records(self, output: str) -> list[VpsDockerHostRecord]:
        """Parse host records from batched output."""
        state_dir = f"{STATE_VOLUME_MOUNT_PATH}/host_state"
        records: list[VpsDockerHostRecord] = []
        for file_path, content in self._split_batched_output(output):
            # Only parse top-level .json files (host records), not agent subdirs
            relative = file_path.removeprefix(f"{state_dir}/")
            if "/" in relative:
                continue
            if not relative.endswith(".json"):
                continue
            host_id_str = relative.removesuffix(".json")
            try:
                host_record = VpsDockerHostRecord.model_validate_json(content)
                host_id = HostId(host_id_str)
                self._cache[host_id] = host_record
                records.append(host_record)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to parse host record {}: {}", file_path, e)
        return records

    def _parse_batched_json_files(self, output: str) -> list[dict[str, Any]]:
        """Parse JSON files from batched output."""
        results: list[dict[str, Any]] = []
        for _file_path, content in self._split_batched_output(output):
            results.append(json.loads(content))
        return results

    def clear_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()
