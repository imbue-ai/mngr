import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostConfig
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# Sentinel marking the start of each agent JSON file in batched-read output.
# Chosen to be a string that cannot appear inside a serialized JSON record.
_AGENT_FILE_SEP: Final[str] = "---MNGR_AGENT_FILE_SEP---"


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

    def read_host_record(self) -> VpsDockerHostRecord | None:
        """Read the host record from the unified volume. Returns None if not present."""
        path = self._host_state_path
        if not self.outer.path_exists(path):
            return None
        try:
            data = self.outer.read_text_file(path)
        except (FileNotFoundError, OSError, MngrError) as e:
            logger.debug("Host record at {} not readable: {}", path, e)
            return None
        try:
            return VpsDockerHostRecord.model_validate_json(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse host record at {}: {}", path, e)
            return None

    def delete_host_record(self) -> None:
        """Delete the host record and all per-agent metadata on the volume."""
        # Remove both files in a single SSH round-trip; -f makes both targets idempotent.
        _run_outer_command(
            self.outer,
            f"rm -rf {shlex.quote(str(self._agents_dir))} {shlex.quote(str(self._host_state_path))}",
            label="delete-host-record",
        )

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
        """Read all persisted agent records on this volume in a single SSH round-trip."""
        agents_dir_q = shlex.quote(str(self._agents_dir))
        # Single shell loop: for each *.json under agents/, print the sentinel,
        # the absolute path, then the file contents. The trailing `|| true`
        # turns a missing directory or empty glob into an empty stdout.
        script = (
            f"for f in {agents_dir_q}/*.json; do "
            f'[ -f "$f" ] || continue; '
            f"echo '{_AGENT_FILE_SEP}'\"$f\"; "
            f'cat "$f"; '
            f"done 2>/dev/null || true"
        )
        result = self.outer.execute_idempotent_command(script)
        return self._parse_batched_agent_records(result.stdout)

    @staticmethod
    def _parse_batched_agent_records(output: str) -> list[dict[str, Any]]:
        """Parse the (sentinel, path, content) chunks produced by list_persisted_agent_data."""
        if not output.strip():
            return []
        agent_records: list[dict[str, Any]] = []
        # Anything before the first sentinel is ignorable noise (e.g. a stray
        # ls warning); the [1:] slice drops it.
        for chunk in output.split(_AGENT_FILE_SEP)[1:]:
            head, _, content = chunk.partition("\n")
            file_path = head.strip()
            if not file_path or not content.strip():
                continue
            try:
                agent_records.append(json.loads(content))
            except json.JSONDecodeError as e:
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
