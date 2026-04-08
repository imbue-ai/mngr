import json
from collections.abc import Mapping
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


class LimaHostConfig(FrozenModel):
    """Configuration for a Lima VM host.

    Stores the Lima instance name and start args so the VM can be
    restarted with the same configuration.
    """

    instance_name: str = Field(description="Lima instance name (e.g. mngr-my-host)")
    start_args: tuple[str, ...] = Field(default=(), description="limactl start arguments for replay")
    image_url: str | None = Field(default=None, description="Image URL used to create the VM")


class HostRecord(FrozenModel):
    """Host metadata stored in the local volume directory.

    This record contains all information needed to connect to and manage a host.
    It is stored at host_state/<host_id>.json in the volume.
    """

    certified_host_data: CertifiedHostData = Field(
        frozen=True,
        description="The certified host data loaded from data.json",
    )
    ssh_hostname: str | None = Field(default=None, description="SSH hostname (usually 127.0.0.1)")
    ssh_port: int | None = Field(default=None, description="SSH port number")
    ssh_user: str | None = Field(default=None, description="SSH user")
    ssh_identity_file: str | None = Field(default=None, description="Path to SSH identity file")
    config: LimaHostConfig | None = Field(default=None, description="Lima VM configuration")
    resources: HostResources | None = Field(default=None, description="Configured host resources")


class LimaHostStore(MutableModel):
    """Host record store backed by a local Volume.

    Stores host records and agent data in the Lima provider's volume directory,
    following the same pattern as Docker's DockerHostStore.

    Directory layout on the volume::

        host_state/
            <host_id>.json
            <host_id>/
                <agent_id>.json
    """

    volume: Volume = Field(frozen=True, description="Volume for storing host state")
    _cache: dict[HostId, HostRecord] = PrivateAttr(default_factory=dict)

    def _host_record_path(self, host_id: HostId) -> str:
        return f"host_state/{host_id}.json"

    def _agent_data_dir(self, host_id: HostId) -> str:
        return f"host_state/{host_id}"

    def _agent_data_path(self, host_id: HostId, agent_id: AgentId) -> str:
        return f"host_state/{host_id}/{agent_id}.json"

    def write_host_record(self, host_record: HostRecord) -> None:
        """Write a host record to the volume."""
        host_id = HostId(host_record.certified_host_data.host_id)
        path = self._host_record_path(host_id)
        data = host_record.model_dump_json(indent=2)
        self.volume.write_files({path: data.encode("utf-8")})
        logger.trace("Wrote host record: {}", path)
        self._cache[host_id] = host_record

    def read_host_record(self, host_id: HostId, use_cache: bool = True) -> HostRecord | None:
        """Read a host record from the volume. Returns None if not found."""
        if use_cache and host_id in self._cache:
            return self._cache[host_id]

        path = self._host_record_path(host_id)
        try:
            data = self.volume.read_file(path)
            host_record = HostRecord.model_validate_json(data)
            self._cache[host_id] = host_record
            return host_record
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to read host record {}: {}", path, e)
            return None

    def delete_host_record(self, host_id: HostId) -> None:
        """Delete a host record and associated agent data from the volume."""
        # Delete agent data files
        agent_dir = self._agent_data_dir(host_id)
        try:
            agent_entries = self.volume.listdir(agent_dir)
            for entry in agent_entries:
                if entry.file_type != VolumeFileType.DIRECTORY:
                    self.volume.remove_file(entry.path)
        except (FileNotFoundError, OSError) as e:
            logger.trace("No agent data to clean up for {}: {}", host_id, e)

        # Delete host record
        path = self._host_record_path(host_id)
        try:
            self.volume.remove_file(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Failed to delete host record {}: {}", host_id, e)

        self._cache.pop(host_id, None)

    def list_all_host_records(self) -> list[HostRecord]:
        """List all host records stored on the volume."""
        records: list[HostRecord] = []
        try:
            entries = self.volume.listdir("host_state")
        except (FileNotFoundError, OSError) as e:
            logger.trace("No host records directory found: {}", e)
            return []

        for entry in entries:
            if entry.file_type != VolumeFileType.FILE or not entry.path.endswith(".json"):
                continue
            filename = entry.path.rsplit("/", 1)[-1]
            host_id_str = filename.removesuffix(".json")
            host_id = HostId(host_id_str)
            record = self.read_host_record(host_id, use_cache=False)
            if record is not None:
                records.append(record)

        return records

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Write agent data for offline listing."""
        agent_id = agent_data.get("id")
        if not agent_id:
            logger.warning("Cannot persist agent data without id field")
            return

        path = self._agent_data_path(host_id, AgentId(str(agent_id)))
        data = json.dumps(dict(agent_data), indent=2)
        self.volume.write_files({path: data.encode("utf-8")})
        logger.trace("Persisted agent data: {}", path)

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        """Read persisted agent data for a host."""
        agent_dir = self._agent_data_dir(host_id)
        try:
            entries = self.volume.listdir(agent_dir)
        except (FileNotFoundError, OSError) as e:
            logger.trace("No agent data directory for host {}: {}", host_id, e)
            return []

        agent_records: list[dict[str, Any]] = []
        for entry in entries:
            if entry.file_type != VolumeFileType.FILE or not entry.path.endswith(".json"):
                continue
            try:
                content = self.volume.read_file(entry.path)
                agent_data = json.loads(content)
                agent_records.append(agent_data)
            except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
                logger.trace("Skipped invalid agent record {}: {}", entry.path, e)
                continue

        return agent_records

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove persisted agent data."""
        path = self._agent_data_path(host_id, agent_id)
        try:
            self.volume.remove_file(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Failed to remove agent data {}: {}", path, e)

    def clear_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()
