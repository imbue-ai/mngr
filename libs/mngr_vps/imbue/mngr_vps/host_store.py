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
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostConfig
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr_vps.primitives import VpsInstanceId

# Sentinel marking the start of each agent JSON file in batched-read output.
# Chosen to be extremely unlikely to appear inside any real agent record:
# long, all-uppercase, dash-delimited, and namespaced with the project tag.
# (It is *not* impossible -- the sentinel is plain ASCII and would be valid
# unescaped inside a JSON string value -- but no realistic agent record
# contains this exact substring.)
_AGENT_FILE_SEP: Final[str] = "---MNGR_AGENT_FILE_SEP---"

# Subdirectory inside the unified volume holding one JSON file per persisted
# agent record (``<agent_id>.json``). Lives next to ``HOST_DIR_SUBPATH`` /
# ``host_state.json`` -- shared with ``instance.py`` so the on-disk layout has
# a single source of truth and renaming the directory requires a single edit.
AGENTS_SUBPATH: Final[str] = "agents"


class VpsHostConfig(HostConfig):
    """VPS-specific host configuration stored in the host record."""

    vps_instance_id: VpsInstanceId = Field(description="Provider-specific VPS instance ID")
    region: str = Field(description="Region where the VPS was created")
    plan: str = Field(description="VPS plan (CPU/RAM specification)")
    start_args: tuple[str, ...] = Field(default=(), description="Docker run arguments for replay on snapshot restore")
    image: str | None = Field(default=None, description="Docker image used for the container")
    container_name: str | None = Field(default=None, description="Docker container name on the VPS (None for bare)")
    volume_name: str | None = Field(
        default=None, description="Per-host unified docker volume name on the VPS (None for bare)"
    )
    vps_ssh_key_id: str | None = Field(default=None, description="Provider SSH key ID (for cleanup on destroy)")


class VpsHostRecord(FrozenModel):
    """Host metadata stored on the VPS unified volume."""

    certified_host_data: CertifiedHostData = Field(frozen=True, description="The certified host data")
    vps_ip: str | None = Field(default=None, description="Current IP address of the VPS")
    ssh_host_public_key: str | None = Field(default=None, description="VPS SSH host public key")
    container_ssh_host_public_key: str | None = Field(default=None, description="Container SSH host public key")
    config: VpsHostConfig | None = Field(default=None, description="VPS and container configuration")
    container_id: str | None = Field(default=None, description="Docker container ID")

    def with_certified_updates(self, *certified_updates: tuple[str, Any]) -> "VpsHostRecord":
        """Return a copy with the given updates applied to the nested ``certified_host_data``.

        ``certified_updates`` are ``to_update`` pairs over the certified data's own
        ``field_ref()`` (e.g. ``updated_at`` / ``stop_reason``). Wraps the
        "update the certified data, then re-wrap the record" idiom so the nested
        copy and the re-wrap can never drift apart at a call site.
        """
        updated_data = self.certified_host_data.model_copy_update(*certified_updates)
        return self.model_copy_update(to_update(self.field_ref().certified_host_data, updated_data))


def _run_outer_command(outer: OuterHostInterface, command: str, *, label: str) -> str:
    """Run a command on the outer host; raise MngrError on non-zero exit."""
    result = outer.execute_idempotent_command(command)
    if not result.success:
        raise MngrError(
            f"VPS outer command {label!r} failed: stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return result.stdout


def resolve_volume_device(outer: OuterHostInterface, volume_name: str) -> Path:
    """Return the bind-source path of ``volume_name`` on the outer.

    The per-host unified volume is created with
    ``docker volume create --driver=local --opt type=none --opt device=<path> --opt o=bind``,
    so the real on-disk storage is wherever ``.Options.device`` points -- the
    docker-managed ``.Mountpoint`` (under ``/var/lib/docker/volumes/<name>/_data``)
    is an unused placeholder. Reading ``.Options.device`` keeps the docker
    volume as the single source of truth for the bind-source path.
    """
    output = _run_outer_command(
        outer,
        f"docker volume inspect {shlex.quote(volume_name)} --format '{{{{.Options.device}}}}'",
        label="docker-volume-inspect",
    )
    device = output.strip()
    if not device:
        raise MngrError(
            f"docker volume inspect returned empty Options.device for {volume_name!r}; "
            "volume may have been created without bind options (--opt type=none --opt device=... --opt o=bind)"
        )
    return Path(device)


class VpsHostStore(MutableModel):
    """Reads/writes one host's metadata over a directory on the outer.

    Each VPS hosts exactly one mngr agent (1:1 invariant), so each store instance
    is bound to a single directory whose backing depends on the realizer:

    - Container realizer: the per-host btrfs subvolume
      (``<btrfs_mount_path>/<host_id_hex>``) that the agent's docker named volume
      binds to. The volume is created with bind options pointing at that
      subvolume, so the docker-managed ``Mountpoint`` placeholder under
      ``/var/lib/docker/volumes`` is never read from -- ``Options.device`` is the
      real path. Construct via :func:`open_host_store`, which resolves that path
      via ``docker volume inspect --format '{{.Options.device}}'``.
    - Bare realizer: a fixed directory on the VM's root disk (no docker volume,
      no btrfs subvolume). Construct directly with that path as ``mountpoint``.

    Either way the layout under ``mountpoint`` is identical, and file operations
    go through the outer host's ``read_text_file`` / ``write_text_file`` /
    ``execute_idempotent_command``::

        host_state.json
        agents/<agent_id>.json
        host_dir/<...agent host data...>
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    outer: OuterHostInterface = Field(frozen=True, description="Outer host used to reach the VPS")
    mountpoint: Path = Field(
        frozen=True,
        description=(
            "Absolute path on the outer of the directory backing this store: the docker volume's "
            "``Options.device`` btrfs subvolume for the container realizer, or the fixed root-disk "
            "store directory for the bare realizer."
        ),
    )

    @property
    def _host_state_path(self) -> Path:
        return self.mountpoint / "host_state.json"

    @property
    def _agents_dir(self) -> Path:
        return self.mountpoint / AGENTS_SUBPATH

    def _agent_data_path(self, agent_id: AgentId) -> Path:
        return self._agents_dir / f"{agent_id}.json"

    def write_host_record(self, host_record: VpsHostRecord) -> None:
        """Write the host record to the unified volume."""
        data = host_record.model_dump_json(indent=2)
        self.outer.write_text_file(self._host_state_path, data)
        logger.trace("Wrote host record at {}", self._host_state_path)

    def read_host_record(self) -> VpsHostRecord | None:
        """Read the host record from the unified volume. Returns None if not present.

        A *missing* host_state.json (e.g. on a freshly-created volume that
        hasn't been finalized yet) returns None. Any other failure --
        transient SSH error, permission problem -- propagates (typically
        as ``HostConnectionError`` for SSH transport failures, or as
        another ``MngrError`` for shell-level errors; both are now
        ``MngrError`` subclasses) so that the outer ``except MngrError``
        guards in callers like ``_read_records_from_vps`` can log a warning
        and fall back to cached records instead of letting the host silently
        disappear from the listing.
        """
        path = self._host_state_path
        if not self.outer.path_exists(path):
            return None
        try:
            data = self.outer.read_text_file(path)
        except OSError as e:
            # File raced from under us between path_exists and read
            # (FileNotFoundError) or a local-outer raised some other
            # OSError. Treat as "missing".
            logger.debug("Host record at {} not readable: {}", path, e)
            return None
        try:
            return VpsHostRecord.model_validate_json(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse host record at {}: {}", path, e)
            return None

    def delete_host_record(self) -> None:
        """Delete the host record and all per-agent metadata on the volume."""
        # Remove the agents/ directory and the host_state.json file in a single
        # SSH round-trip. ``-r`` is required because agents/ is a directory;
        # ``-f`` makes both targets idempotent (no error if either is missing).
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
        """Read all persisted agent records on this volume in a single SSH round-trip.

        The shell script short-circuits with exit 0 + empty stdout when
        the agents/ directory does not exist yet (brand-new volume that
        hasn't persisted any agent data). All *other* failures -- permission
        denied, OOM, malformed shell -- propagate as ``MngrError`` rather
        than being silently turned into an empty list.
        """
        agents_dir_q = shlex.quote(str(self._agents_dir))
        # Single shell call: a directory-missing guard up front, then a
        # loop over agents/*.json. The `[ -f "$f" ] || continue` guard
        # turns the literal-glob fallback (when no files match) into a
        # clean exit-0 with empty stdout, without swallowing real errors.
        script = (
            f"[ -d {agents_dir_q} ] || exit 0; "
            f"for f in {agents_dir_q}/*.json; do "
            f'[ -f "$f" ] || continue; '
            f"echo '{_AGENT_FILE_SEP}'\"$f\"; "
            f'cat "$f"; '
            f"done"
        )
        output = _run_outer_command(self.outer, script, label="list-agent-records")
        return self._parse_batched_agent_records(output)

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


def open_host_store(outer: OuterHostInterface, volume_name: str) -> VpsHostStore:
    """Resolve ``volume_name``'s bind-source path on ``outer`` and bind a store to it.

    The store's underlying directory is the btrfs subvolume the docker volume's
    ``Options.device`` points at, not the unused docker-managed
    ``/var/lib/docker/volumes/<name>/_data`` placeholder.

    Raises ``MngrError`` if the volume does not exist on the outer (which
    means the host was never finalized or has already been destroyed) or
    does not carry the expected bind options.
    """
    device_path = resolve_volume_device(outer, volume_name)
    return VpsHostStore(outer=outer, mountpoint=device_path)
