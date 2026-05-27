import json
import shlex
from pathlib import Path
from typing import Any
from typing import Mapping

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostConfig
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr_vps_docker.primitives import VpsInstanceId


class VpsHostConfig(HostConfig):
    """VPS-specific host configuration stored in the host record."""

    vps_instance_id: VpsInstanceId = Field(description="Provider-specific VPS instance ID")
    region: str = Field(description="Region where the VPS was created")
    plan: str = Field(description="VPS plan (CPU/RAM specification)")
    os_id: int | str = Field(
        description=(
            "OS image identifier used to create the VPS. Integer for providers "
            "like Vultr; string image name for providers like OVH classic VPS."
        ),
    )
    start_args: tuple[str, ...] = Field(default=(), description="Docker run arguments for replay on snapshot restore")
    image: str | None = Field(default=None, description="Docker image used for the container")
    container_name: str = Field(description="Docker container name on the VPS")
    volume_name: str = Field(description="Docker volume name on the VPS")
    vps_ssh_key_id: str | None = Field(default=None, description="Provider SSH key ID (for cleanup on destroy)")


class VpsDockerHostRecord(FrozenModel):
    """Host metadata stored on the VPS unified volume."""

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


def resolve_volume_mountpoint(outer: OuterHostInterface, volume_name: str) -> Path:
    """Return the absolute filesystem path where docker has mounted ``volume_name`` on the outer."""
    output = _run_outer_command(
        outer,
        f"docker volume inspect {shlex.quote(volume_name)} --format '{{{{.Mountpoint}}}}'",
        label="docker-volume-inspect",
    )
    mountpoint = output.strip()
    if not mountpoint:
        raise MngrError(f"docker volume inspect returned empty mountpoint for {volume_name!r}")
    return Path(mountpoint)


def create_volume_with_layout(outer: OuterHostInterface, volume_name: str, host_dir_subpath: str = "host_dir") -> Path:
    """Create the unified host volume and seed its directory layout.

    Creates the named Docker volume (idempotent) and pre-creates the
    ``<mountpoint>/<host_dir_subpath>`` and ``<mountpoint>/agents``
    subdirectories so that callers writing to either of those locations
    don't have to mkdir first. Returns the resolved mountpoint.
    """
    _run_outer_command(
        outer,
        f"docker volume create {shlex.quote(volume_name)}",
        label="docker-volume-create",
    )
    mountpoint = resolve_volume_mountpoint(outer, volume_name)
    _run_outer_command(
        outer,
        f"mkdir -p {shlex.quote(str(mountpoint / host_dir_subpath))} {shlex.quote(str(mountpoint / 'agents'))}",
        label="seed-volume-layout",
    )
    return mountpoint


class VpsDockerHostStore(MutableModel):
    """Reads/writes one host's metadata directly on its unified Docker volume.

    Each VPS hosts exactly one mngr container (1:1 invariant), so each store
    instance is bound to a single volume's mountpoint on the outer (typically
    ``/var/lib/docker/volumes/<volume_name>/_data``). File operations go
    through the outer host's ``read_text_file`` / ``write_text_file`` /
    ``execute_idempotent_command``.

    Layout inside the volume mountpoint::

        host_state.json
        agents/<agent_id>.json
        host_dir/<...agent host data...>

    Construct via :func:`open_host_store`, which resolves the volume's
    mountpoint via ``docker volume inspect``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    outer: OuterHostInterface = Field(frozen=True, description="Outer host used to reach the VPS")
    mountpoint: Path = Field(frozen=True, description="Absolute path on the outer where the host volume is mounted")

    _record_cache: VpsDockerHostRecord | None = PrivateAttr(default=None)

    @property
    def _host_state_path(self) -> Path:
        return self.mountpoint / "host_state.json"

    @property
    def _agents_dir(self) -> Path:
        return self.mountpoint / "agents"

    def _agent_data_path(self, agent_id: AgentId) -> Path:
        return self._agents_dir / f"{agent_id}.json"

    def write_host_record(self, host_record: VpsDockerHostRecord) -> None:
        """Write the host record to the unified volume."""
        data = host_record.model_dump_json(indent=2)
        self.outer.write_text_file(self._host_state_path, data)
        logger.trace("Wrote host record at {}", self._host_state_path)
        self._record_cache = host_record

    def read_host_record(self, is_cache_enabled: bool = True) -> VpsDockerHostRecord | None:
        """Read the host record from the unified volume. Returns None if not present."""
        if is_cache_enabled and self._record_cache is not None:
            return self._record_cache

        path = self._host_state_path
        if not self.outer.path_exists(path):
            return None
        try:
            data = self.outer.read_text_file(path)
        except (FileNotFoundError, OSError, MngrError) as e:
            logger.debug("Host record at {} not readable: {}", path, e)
            return None
        try:
            host_record = VpsDockerHostRecord.model_validate_json(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse host record at {}: {}", path, e)
            return None
        self._record_cache = host_record
        return host_record

    def delete_host_record(self) -> None:
        """Delete the host record and all per-agent metadata on the volume."""
        # Remove both files in a single SSH round-trip; -f makes both targets idempotent.
        _run_outer_command(
            self.outer,
            f"rm -rf {shlex.quote(str(self._agents_dir))} {shlex.quote(str(self._host_state_path))}",
            label="delete-host-record",
        )
        self._record_cache = None

    def persist_agent_data(self, agent_data: Mapping[str, object]) -> None:
        """Write agent data for offline listing."""
        agent_id_value = agent_data.get("id")
        if not agent_id_value:
            logger.warning("Cannot persist agent data without id field")
            return

        path = self._agent_data_path(AgentId(str(agent_id_value)))
        data = json.dumps(dict(agent_data), indent=2)
        self.outer.write_text_file(path, data)
        logger.trace("Persisted agent data at {}", path)

    def list_persisted_agent_data(self) -> list[dict[str, Any]]:
        """Read all persisted agent records on this volume."""
        result = self.outer.execute_idempotent_command(
            f"ls -1 {shlex.quote(str(self._agents_dir))}/*.json 2>/dev/null || true"
        )
        # Empty / missing dir produces empty stdout via the `|| true` fallback.
        agent_records: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            file_path = line.strip()
            if not file_path:
                continue
            try:
                content = self.outer.read_text_file(Path(file_path))
                agent_records.append(json.loads(content))
            except (FileNotFoundError, OSError, MngrError, json.JSONDecodeError) as e:
                logger.warning("Skipped invalid agent record {}: {}", file_path, e)
        return agent_records

    def remove_persisted_agent_data(self, agent_id: AgentId) -> None:
        """Remove a single agent's persisted data."""
        path = self._agent_data_path(agent_id)
        try:
            _run_outer_command(self.outer, f"rm -f {shlex.quote(str(path))}", label="remove-agent-data")
        except MngrError as e:
            logger.warning("Failed to remove agent data {}: {}", path, e)


def open_host_store(outer: OuterHostInterface, volume_name: str) -> VpsDockerHostStore:
    """Resolve ``volume_name``'s mountpoint on ``outer`` and bind a store to it.

    Raises ``MngrError`` if the volume does not exist on the outer (which
    means the host was never finalized or has already been destroyed).
    """
    mountpoint = resolve_volume_mountpoint(outer, volume_name)
    return VpsDockerHostStore(outer=outer, mountpoint=mountpoint)
