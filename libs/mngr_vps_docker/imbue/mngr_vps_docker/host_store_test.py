"""Tests for VPS Docker host store data types and volume-backed I/O."""

import json
import shlex
import subprocess
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pyinfra.api import Host as PyinfraHost

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsDockerHostStore
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.host_store import create_volume_with_layout
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.host_store import resolve_volume_mountpoint
from imbue.mngr_vps_docker.primitives import VpsInstanceId

_AGENT_ID_1 = AgentId.generate()
_AGENT_ID_2 = AgentId.generate()
_AGENT_ID_3 = AgentId.generate()


def _make_certified_data(host_id: str = "test-host-123", host_name: str = "test-host") -> CertifiedHostData:
    """Create a minimal CertifiedHostData for testing."""
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=host_id,
        host_name=host_name,
        idle_timeout_seconds=800,
        activity_sources=(),
        image="debian:bookworm-slim",
        user_tags={},
        created_at=now,
        updated_at=now,
    )


# =============================================================================
# Schema tests: VpsHostConfig and VpsDockerHostRecord are unchanged by the
# unified-volume refactor; these tests guard the pydantic shapes.
# =============================================================================


def test_vps_host_config_creation() -> None:
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="mngr-test-host",
        volume_name="mngr-host-vol-abc123",
    )
    assert config.vps_instance_id == VpsInstanceId("inst-abc123")
    assert config.region == "ewr"
    assert config.plan == "vc2-1c-1gb"
    assert config.os_id == 2136
    assert config.container_name == "mngr-test-host"
    assert config.volume_name == "mngr-host-vol-abc123"


def test_vps_host_config_optional_fields() -> None:
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="test",
        volume_name="vol",
    )
    assert config.start_args == ()
    assert config.image is None
    assert config.vps_ssh_key_id is None


def test_vps_docker_host_record_creation() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="192.168.1.100",
        ssh_host_public_key="ssh-ed25519 AAAA vps-host-key",
        container_ssh_host_public_key="ssh-ed25519 BBBB container-host-key",
    )
    assert record.certified_host_data.host_id == "test-host-123"
    assert record.vps_ip == "192.168.1.100"
    assert record.ssh_host_public_key == "ssh-ed25519 AAAA vps-host-key"


def test_vps_docker_host_record_optional_fields() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(certified_host_data=certified_data)
    assert record.vps_ip is None
    assert record.ssh_host_public_key is None
    assert record.container_ssh_host_public_key is None
    assert record.config is None
    assert record.container_id is None


def test_vps_docker_host_record_serialization_roundtrip() -> None:
    certified_data = _make_certified_data()
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="test",
        volume_name="vol",
    )
    original = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
        config=config,
        container_id="deadbeef1234",
    )

    json_str = original.model_dump_json()
    restored = VpsDockerHostRecord.model_validate_json(json_str)

    assert restored.certified_host_data.host_id == "test-host-123"
    assert restored.vps_ip == "10.0.0.1"
    assert restored.config is not None
    assert restored.config.vps_instance_id == VpsInstanceId("inst-abc123")
    assert restored.container_id == "deadbeef1234"


def test_vps_docker_host_record_model_copy() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
    )
    new_data = _make_certified_data(host_name="updated-host")
    updated = record.model_copy(update={"certified_host_data": new_data})
    assert updated.certified_host_data.host_name == "updated-host"
    assert updated.vps_ip == "10.0.0.1"
    # Original unchanged.
    assert record.certified_host_data.host_name == "test-host"


# =============================================================================
# VpsDockerHostStore tests against a tmp-dir-backed fake outer.
#
# The fake stands in for the VPS's outer host: file I/O goes to a real local
# tmp_path (so write_text_file/read_text_file exercise real bytes), and
# execute_idempotent_command shells out locally for the few shell primitives
# the store actually uses (mkdir, rm, ls, docker volume inspect).
#
# docker volume inspect is special-cased to return the tmp_path so
# resolve_volume_mountpoint resolves to the fake mountpoint without needing
# a real docker daemon.
# =============================================================================


class _LocalFakeOuter(OuterHostInterface):
    """An OuterHostInterface stand-in backed by a local tmp directory.

    Only the methods VpsDockerHostStore (and its create-helpers) call are
    implemented in any meaningful way. ``mountpoint_by_volume`` lets the
    fake answer ``docker volume inspect <name>`` queries deterministically.
    The remaining ``@abstractmethod`` methods on OuterHostInterface raise
    so the fake fails loudly if a test exercises an unsupported path.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mountpoint_by_volume: dict[str, Path] = Field(default_factory=dict)
    recorded_commands: list[str] = Field(default_factory=list)

    @property
    def is_local(self) -> bool:
        return True

    def get_name(self) -> str:
        return "local-fake-outer"

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)

        # Intercept docker volume inspect so we don't need a real daemon.
        if command.startswith("docker volume inspect "):
            tokens = shlex.split(command)
            volume_name = tokens[3]
            mountpoint = self.mountpoint_by_volume.get(volume_name)
            if mountpoint is None:
                return CommandResult(
                    stdout="",
                    stderr=f"Error: No such volume: {volume_name}",
                    success=False,
                )
            return CommandResult(stdout=f"{mountpoint}\n", stderr="", success=True)

        if command.startswith("docker volume create "):
            tokens = shlex.split(command)
            volume_name = tokens[3]
            # Provision a local directory to stand in for the volume's _data dir.
            if volume_name not in self.mountpoint_by_volume:
                raise RuntimeError(
                    f"Test setup error: must register a mountpoint for {volume_name!r} before "
                    f"create_volume_with_layout is called."
                )
            self.mountpoint_by_volume[volume_name].mkdir(parents=True, exist_ok=True)
            return CommandResult(stdout=volume_name, stderr="", success=True)

        # Shell out for the remaining primitives (mkdir / rm / ls / test).
        completed = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            success=completed.returncode == 0,
        )

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError("_LocalFakeOuter.execute_stateful_command not used by VpsDockerHostStore")

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError("_LocalFakeOuter.execute_streaming_command not used by VpsDockerHostStore")

    def read_file(self, path: Path) -> bytes:
        return path.read_bytes()

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)

    def get_file_mtime(self, path: Path) -> datetime | None:
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        return None

    def path_exists(self, path: Path) -> bool:
        return path.exists()


def _make_local_connector() -> PyinfraConnector:
    """Construct a no-op PyinfraConnector so OuterHostInterface's frozen field validates."""
    return PyinfraConnector(cast(PyinfraHost, object()))


def _outer_with_mountpoint(mountpoint: Path, volume_name: str = "mngr-host-vol-test") -> _LocalFakeOuter:
    return _LocalFakeOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        mountpoint_by_volume={volume_name: mountpoint},
    )


def _make_store(mountpoint: Path) -> VpsDockerHostStore:
    outer = _LocalFakeOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        mountpoint_by_volume={"unused": mountpoint},
    )
    return VpsDockerHostStore(outer=outer, mountpoint=mountpoint)


def test_resolve_volume_mountpoint_returns_inspected_path(tmp_path: Path) -> None:
    outer = _outer_with_mountpoint(tmp_path / "_data", volume_name="mngr-host-vol-aaaa")
    result = resolve_volume_mountpoint(cast(OuterHostInterface, outer), "mngr-host-vol-aaaa")
    assert result == tmp_path / "_data"


def test_resolve_volume_mountpoint_raises_when_volume_missing(tmp_path: Path) -> None:
    outer = _outer_with_mountpoint(tmp_path / "_data", volume_name="mngr-host-vol-aaaa")
    with pytest.raises(MngrError, match="docker-volume-inspect"):
        resolve_volume_mountpoint(cast(OuterHostInterface, outer), "mngr-host-vol-does-not-exist")


def test_create_volume_with_layout_seeds_host_dir_and_agents(tmp_path: Path) -> None:
    mountpoint = tmp_path / "_data"
    outer = _outer_with_mountpoint(mountpoint, volume_name="mngr-host-vol-aaaa")
    returned = create_volume_with_layout(cast(OuterHostInterface, outer), "mngr-host-vol-aaaa")
    assert returned == mountpoint
    assert (mountpoint / "host_dir").is_dir()
    assert (mountpoint / "agents").is_dir()


def test_open_host_store_resolves_mountpoint(tmp_path: Path) -> None:
    mountpoint = tmp_path / "_data"
    mountpoint.mkdir()
    outer = _outer_with_mountpoint(mountpoint, volume_name="mngr-host-vol-aaaa")
    store = open_host_store(cast(OuterHostInterface, outer), "mngr-host-vol-aaaa")
    assert store.mountpoint == mountpoint


def test_write_then_read_host_record_roundtrips(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = VpsDockerHostRecord(
        certified_host_data=_make_certified_data(host_id="host-abc", host_name="alpha"),
        vps_ip="10.0.0.1",
    )

    store.write_host_record(record)

    # On disk the record lives at the volume root.
    on_disk = (tmp_path / "host_state.json").read_text()
    assert json.loads(on_disk)["certified_host_data"]["host_id"] == "host-abc"

    # Reading the record back, bypassing the in-memory cache.
    fresh_store = _make_store(tmp_path)
    loaded = fresh_store.read_host_record(is_cache_enabled=False)
    assert loaded is not None
    assert loaded.certified_host_data.host_name == "alpha"
    assert loaded.vps_ip == "10.0.0.1"


def test_read_host_record_returns_none_when_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.read_host_record(is_cache_enabled=False) is None


def test_read_host_record_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "host_state.json").write_text("{not valid json")
    store = _make_store(tmp_path)
    assert store.read_host_record(is_cache_enabled=False) is None


def test_read_host_record_uses_cache_after_write(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = VpsDockerHostRecord(certified_host_data=_make_certified_data())
    store.write_host_record(record)
    # Removing the on-disk file should not affect the cached read.
    (tmp_path / "host_state.json").unlink()
    cached = store.read_host_record(is_cache_enabled=True)
    assert cached is not None
    assert cached.certified_host_data.host_id == "test-host-123"


def test_delete_host_record_removes_state_and_agents(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = VpsDockerHostRecord(certified_host_data=_make_certified_data())
    store.write_host_record(record)
    store.persist_agent_data({"id": str(_AGENT_ID_1), "name": "first"})
    store.persist_agent_data({"id": str(_AGENT_ID_2), "name": "second"})

    store.delete_host_record()

    assert not (tmp_path / "host_state.json").exists()
    assert not (tmp_path / "agents").exists()
    # Cache cleared.
    assert store.read_host_record(is_cache_enabled=True) is None


def test_delete_host_record_is_idempotent_when_nothing_exists(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # No prior write; deletion should still succeed.
    store.delete_host_record()


def test_persist_then_list_agent_data_roundtrips(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.persist_agent_data({"id": str(_AGENT_ID_1), "name": "first"})
    store.persist_agent_data({"id": str(_AGENT_ID_2), "name": "second"})

    listed = store.list_persisted_agent_data()
    listed_by_id = {entry["id"]: entry["name"] for entry in listed}
    assert listed_by_id == {str(_AGENT_ID_1): "first", str(_AGENT_ID_2): "second"}


def test_persist_agent_data_without_id_is_a_noop(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.persist_agent_data({"name": "no-id-here"})
    # Nothing was written.
    assert store.list_persisted_agent_data() == []


def test_list_persisted_agent_data_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.list_persisted_agent_data() == []


def test_list_persisted_agent_data_skips_corrupt_entries(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.persist_agent_data({"id": str(_AGENT_ID_1), "name": "valid"})

    # Inject a corrupt sibling file (filename need not be a valid AgentId).
    corrupt = tmp_path / "agents" / "agent-bad.json"
    corrupt.write_text("{not valid json")

    listed = store.list_persisted_agent_data()
    assert len(listed) == 1
    assert listed[0]["id"] == str(_AGENT_ID_1)


def test_remove_persisted_agent_data_deletes_only_target(tmp_path: Path) -> None:
    keep_id = _AGENT_ID_1
    drop_id = _AGENT_ID_2
    store = _make_store(tmp_path)
    store.persist_agent_data({"id": str(keep_id), "name": "keep"})
    store.persist_agent_data({"id": str(drop_id), "name": "drop"})

    store.remove_persisted_agent_data(drop_id)

    remaining_ids = {entry["id"] for entry in store.list_persisted_agent_data()}
    assert remaining_ids == {str(keep_id)}
    assert not (tmp_path / "agents" / f"{drop_id}.json").exists()


def test_remove_persisted_agent_data_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # No prior persist; removal must not raise.
    store.remove_persisted_agent_data(_AGENT_ID_3)
