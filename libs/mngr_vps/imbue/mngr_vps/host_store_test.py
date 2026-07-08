"""Tests for VPS host store data types and volume-backed I/O."""

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

from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.host_store import open_host_store
from imbue.mngr_vps.host_store import resolve_volume_device
from imbue.mngr_vps.primitives import VpsInstanceId

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
# Schema tests: VpsHostConfig and VpsHostRecord are unchanged by the
# unified-volume refactor; these tests guard the pydantic shapes.
# =============================================================================


def test_vps_host_config_optional_fields() -> None:
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        container_name="test",
        volume_name="vol",
    )
    assert config.start_args == ()
    assert config.image is None
    assert config.vps_ssh_key_id is None


def test_vps_host_record_optional_fields() -> None:
    certified_data = _make_certified_data()
    record = VpsHostRecord(certified_host_data=certified_data)
    assert record.vps_ip is None
    assert record.ssh_host_public_key is None
    assert record.container_ssh_host_public_key is None
    assert record.config is None
    assert record.container_id is None


def test_vps_host_record_serialization_roundtrip() -> None:
    certified_data = _make_certified_data()
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        container_name="test",
        volume_name="vol",
    )
    original = VpsHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
        config=config,
        container_id="deadbeef1234",
    )

    json_str = original.model_dump_json()
    restored = VpsHostRecord.model_validate_json(json_str)

    assert restored.certified_host_data.host_id == "test-host-123"
    assert restored.vps_ip == "10.0.0.1"
    assert restored.config is not None
    assert restored.config.vps_instance_id == VpsInstanceId("inst-abc123")
    assert restored.container_id == "deadbeef1234"


def test_vps_host_record_model_copy_update() -> None:
    certified_data = _make_certified_data()
    record = VpsHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
    )
    new_data = _make_certified_data(host_name="updated-host")
    updated = record.model_copy_update(to_update(record.field_ref().certified_host_data, new_data))
    assert updated.certified_host_data.host_name == "updated-host"
    assert updated.vps_ip == "10.0.0.1"
    # Original unchanged.
    assert record.certified_host_data.host_name == "test-host"


# =============================================================================
# VpsHostStore tests against a tmp-dir-backed fake outer.
#
# The fake stands in for the VPS's outer host: file I/O goes to a real local
# tmp_path (so write_text_file/read_text_file exercise real bytes), and
# execute_idempotent_command shells out locally for the few shell primitives
# the store actually uses (mkdir, rm, ls, docker volume inspect).
#
# docker volume inspect is special-cased to return the registered
# ``device_by_volume`` path so ``resolve_volume_device`` resolves to the fake
# bind-source path without needing a real docker daemon.
# =============================================================================


class _LocalFakeOuter(OuterHostInterface):
    """An OuterHostInterface stand-in backed by a local tmp directory.

    Only the methods VpsHostStore calls are implemented in any
    meaningful way. ``device_by_volume`` lets the fake answer
    ``docker volume inspect --format '{{.Options.device}}'`` queries
    deterministically. The remaining ``@abstractmethod`` methods on
    OuterHostInterface raise so the fake fails loudly if a test exercises an
    unsupported path.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    device_by_volume: dict[str, Path] = Field(default_factory=dict)
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
            device = self.device_by_volume.get(volume_name)
            if device is None:
                return CommandResult(
                    stdout="",
                    stderr=f"Error: No such volume: {volume_name}",
                    success=False,
                )
            return CommandResult(stdout=f"{device}\n", stderr="", success=True)

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
        raise NotImplementedError("_LocalFakeOuter.execute_stateful_command not used by VpsHostStore")

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError("_LocalFakeOuter.execute_streaming_command not used by VpsHostStore")

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

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        raise NotImplementedError("_LocalFakeOuter.list_directory not used by VpsHostStore")

    # path_exists is inherited from OuterHostInterface, which already routes to
    # Path.exists() when is_local is True.


def _make_local_connector() -> PyinfraConnector:
    """Construct a no-op PyinfraConnector so OuterHostInterface's frozen field validates."""
    return PyinfraConnector(cast(PyinfraHost, object()))


def _outer_with_device(device: Path, volume_name: str = "mngr-host-vol-test") -> _LocalFakeOuter:
    return _LocalFakeOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        device_by_volume={volume_name: device},
    )


def _make_store(mountpoint: Path) -> VpsHostStore:
    outer = _LocalFakeOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        device_by_volume={"unused": mountpoint},
    )
    return VpsHostStore(outer=outer, mountpoint=mountpoint)


def test_resolve_volume_device_returns_inspected_device_path(tmp_path: Path) -> None:
    outer = _outer_with_device(tmp_path / "subvol", volume_name="mngr-host-vol-aaaa")
    result = resolve_volume_device(cast(OuterHostInterface, outer), "mngr-host-vol-aaaa")
    assert result == tmp_path / "subvol"


def test_resolve_volume_device_raises_when_volume_missing(tmp_path: Path) -> None:
    outer = _outer_with_device(tmp_path / "subvol", volume_name="mngr-host-vol-aaaa")
    with pytest.raises(MngrError, match="docker-volume-inspect"):
        resolve_volume_device(cast(OuterHostInterface, outer), "mngr-host-vol-does-not-exist")


def test_resolve_volume_device_raises_when_options_device_empty() -> None:
    """A volume created without bind options (no ``Options.device``) must fail loudly."""

    class _EmptyDeviceOuter(_LocalFakeOuter):
        def execute_idempotent_command(
            self,
            command: str,
            user: str | None = None,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            timeout_seconds: float | None = None,
        ) -> CommandResult:
            self.recorded_commands.append(command)
            if command.startswith("docker volume inspect "):
                # Docker's text/template prints "<no value>" for a missing
                # nested field; an empty options map prints as an empty string.
                return CommandResult(stdout="\n", stderr="", success=True)
            return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds)

    outer = _EmptyDeviceOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        device_by_volume={},
    )
    with pytest.raises(MngrError, match="empty Options.device"):
        resolve_volume_device(cast(OuterHostInterface, outer), "mngr-host-vol-no-bind-opts")


def test_open_host_store_binds_to_inspected_device_path(tmp_path: Path) -> None:
    # open_host_store only consults the stubbed `docker volume inspect`; no
    # real directory is required for resolving the bind-source path.
    device_path = tmp_path / "subvol"
    outer = _outer_with_device(device_path, volume_name="mngr-host-vol-aaaa")
    store = open_host_store(cast(OuterHostInterface, outer), "mngr-host-vol-aaaa")
    assert store.mountpoint == device_path


def test_write_then_read_host_record_roundtrips(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = VpsHostRecord(
        certified_host_data=_make_certified_data(host_id="host-abc", host_name="alpha"),
        vps_ip="10.0.0.1",
    )

    store.write_host_record(record)

    # On disk the record lives at the volume root.
    on_disk = (tmp_path / "host_state.json").read_text()
    assert json.loads(on_disk)["certified_host_data"]["host_id"] == "host-abc"

    # Reading the record back from a fresh store reflects what's on disk.
    fresh_store = _make_store(tmp_path)
    loaded = fresh_store.read_host_record()
    assert loaded is not None
    assert loaded.certified_host_data.host_name == "alpha"
    assert loaded.vps_ip == "10.0.0.1"


def test_read_host_record_returns_none_when_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.read_host_record() is None


def test_read_host_record_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "host_state.json").write_text("{not valid json")
    store = _make_store(tmp_path)
    assert store.read_host_record() is None


def test_read_host_record_propagates_mngr_error(tmp_path: Path) -> None:
    """Transient SSH-class read failures must NOT be swallowed.

    If read_text_file raises MngrError, callers like ``_read_records_from_vps``
    rely on the exception bubbling out so they can warn and fall back to
    cached records. Silently returning None would make the host disappear
    from the listing on any flaky-network blip.
    """

    class _FailingReadOuter(_LocalFakeOuter):
        def path_exists(self, path: Path) -> bool:
            return True

        def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
            raise MngrError("simulated transient SSH failure")

    outer = _FailingReadOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        device_by_volume={"unused": tmp_path},
    )
    store = VpsHostStore(outer=outer, mountpoint=tmp_path)
    with pytest.raises(MngrError, match="simulated transient SSH failure"):
        store.read_host_record()


def test_delete_host_record_removes_state_and_agents(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = VpsHostRecord(certified_host_data=_make_certified_data())
    store.write_host_record(record)
    store.persist_agent_data({"id": str(_AGENT_ID_1), "name": "first"})
    store.persist_agent_data({"id": str(_AGENT_ID_2), "name": "second"})

    store.delete_host_record()

    assert not (tmp_path / "host_state.json").exists()
    assert not (tmp_path / "agents").exists()
    # Subsequent read sees no record.
    assert store.read_host_record() is None


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


def test_list_persisted_agent_data_raises_on_shell_failure(tmp_path: Path) -> None:
    """A non-zero exit from the batched agent read must raise instead of returning []."""

    class _FailingShellOuter(_LocalFakeOuter):
        def execute_idempotent_command(
            self,
            command: str,
            user: str | None = None,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            timeout_seconds: float | None = None,
        ) -> CommandResult:
            # Let path_exists / docker volume inspect work normally; fail
            # only the batched agent-list shell loop.
            if "for f in" in command:
                return CommandResult(stdout="", stderr="permission denied", success=False)
            return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds)

    (tmp_path / "agents").mkdir()
    outer = _FailingShellOuter(
        id=HostId.generate(),
        connector=_make_local_connector(),
        device_by_volume={"unused": tmp_path},
    )
    store = VpsHostStore(outer=outer, mountpoint=tmp_path)
    with pytest.raises(MngrError, match="list-agent-records"):
        store.list_persisted_agent_data()


def test_list_persisted_agent_data_reads_all_agents_in_one_round_trip(tmp_path: Path) -> None:
    """The agent list comes back in a single ``execute_idempotent_command`` call, and
    that call count must not grow with the number of agents.

    The assertion on call count is deliberate, not implementation coupling: each
    ``execute_idempotent_command`` is a (slow) network round-trip to the VPS, so
    "one batched read regardless of agent count" is a real performance contract.
    A regression to one stat/read per agent would flip exactly this test. Comparing
    two different agent counts (rather than pinning a bare literal) pins the
    *does-not-scale* property, which is what actually matters.
    """

    def _read_call_delta(volume_dir: Path, agent_count: int) -> tuple[int, set[str]]:
        volume_dir.mkdir()
        outer = _LocalFakeOuter(
            id=HostId.generate(),
            connector=_make_local_connector(),
            device_by_volume={"unused": volume_dir},
        )
        store = VpsHostStore(outer=outer, mountpoint=volume_dir)
        ids = {str(AgentId.generate()) for _ in range(agent_count)}
        for agent_id in ids:
            store.persist_agent_data({"id": agent_id, "name": agent_id})
        pre_call_count = len(outer.recorded_commands)
        listed = store.list_persisted_agent_data()
        post_call_count = len(outer.recorded_commands)
        return post_call_count - pre_call_count, {entry["id"] for entry in listed}

    delta_two, ids_two = _read_call_delta(tmp_path / "vol-two", 2)
    delta_five, ids_five = _read_call_delta(tmp_path / "vol-five", 5)

    assert len(ids_two) == 2
    assert len(ids_five) == 5
    # Exactly one batched read, and identical regardless of how many agents exist.
    assert delta_two == 1
    assert delta_five == 1
