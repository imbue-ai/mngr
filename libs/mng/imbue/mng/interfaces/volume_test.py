from typing import Mapping

import pytest

from imbue.mng.interfaces.data_types import VolumeFile
from imbue.mng.interfaces.data_types import VolumeFileType
from imbue.mng.interfaces.volume import BaseVolume
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.interfaces.volume import ScopedVolume
from imbue.mng.interfaces.volume import _scoped_path
from imbue.mng.primitives import AgentId


class InMemoryVolume(BaseVolume):
    """In-memory volume implementation for testing."""

    files: dict[str, bytes] = {}

    def listdir(self, path: str) -> list[VolumeFile]:
        path = path.rstrip("/")
        results: list[VolumeFile] = []
        for file_path in sorted(self.files):
            parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
            if parent == path or (not path and "/" not in file_path):
                results.append(
                    VolumeFile(path=file_path, file_type=VolumeFileType.FILE, mtime=0, size=len(self.files[file_path]))
                )
        return results

    def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        if not recursive:
            if path not in self.files:
                raise FileNotFoundError(path)
            del self.files[path]
            return
        prefix = path.rstrip("/") + "/"
        to_delete = [k for k in self.files if k == path or k.startswith(prefix)]
        if not to_delete:
            raise FileNotFoundError(path)
        for k in to_delete:
            del self.files[k]

    def remove_directory(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        to_delete = [k for k in self.files if k.startswith(prefix) or k == path.rstrip("/")]
        for k in to_delete:
            del self.files[k]

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        self.files.update(file_contents_by_path)


# =============================================================================
# _scoped_path tests
# =============================================================================


def test_scoped_path_prepends_prefix() -> None:
    assert _scoped_path("/data", "file.txt") == "/data/file.txt"


def test_scoped_path_strips_leading_slash_from_path() -> None:
    assert _scoped_path("/data", "/file.txt") == "/data/file.txt"


def test_scoped_path_returns_prefix_for_empty_path() -> None:
    assert _scoped_path("/data", "") == "/data"


def test_scoped_path_returns_prefix_for_slash_only() -> None:
    assert _scoped_path("/data", "/") == "/data"


def test_scoped_path_handles_nested_paths() -> None:
    assert _scoped_path("/data", "sub/dir/file.txt") == "/data/sub/dir/file.txt"


# =============================================================================
# BaseVolume.scoped tests
# =============================================================================


def test_base_volume_scoped_returns_scoped_volume() -> None:
    vol = InMemoryVolume(files={"/host/file.txt": b"hello"})
    scoped = vol.scoped("/host")
    assert isinstance(scoped, ScopedVolume)


# =============================================================================
# ScopedVolume tests
# =============================================================================


@pytest.fixture()
def volume_with_files() -> InMemoryVolume:
    return InMemoryVolume(
        files={
            "/host/data.json": b'{"key": "value"}',
            "/host/agents/a1.json": b'{"id": "a1"}',
            "/host/agents/a2.json": b'{"id": "a2"}',
            "/other/file.txt": b"other",
        }
    )


def test_scoped_volume_read_file(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    assert scoped.read_file("data.json") == b'{"key": "value"}'


def test_scoped_volume_read_file_strips_leading_slash(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    assert scoped.read_file("/data.json") == b'{"key": "value"}'


def test_scoped_volume_write_files(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    scoped.write_files({"new.txt": b"new content"})
    assert volume_with_files.files["/host/new.txt"] == b"new content"


def test_scoped_volume_remove_file(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    scoped.remove_file("data.json")
    assert "/host/data.json" not in volume_with_files.files


def test_scoped_volume_remove_file_recursive(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    scoped.remove_file("agents", recursive=True)
    assert "/host/agents/a1.json" not in volume_with_files.files
    assert "/host/agents/a2.json" not in volume_with_files.files
    # non-agents files should remain
    assert "/host/data.json" in volume_with_files.files
    assert "/other/file.txt" in volume_with_files.files


def test_remove_file_recursive_deletes_path_and_children() -> None:
    vol = InMemoryVolume(
        files={
            "/host/agent1.json": b"a1",
            "/host/agent2.json": b"a2",
            "/other.json": b"other",
        }
    )
    vol.remove_file("/host", recursive=True)
    assert "/host/agent1.json" not in vol.files
    assert "/host/agent2.json" not in vol.files
    assert "/other.json" in vol.files


def test_remove_file_recursive_nonexistent_raises() -> None:
    vol = InMemoryVolume(files={"/existing.json": b"data"})
    with pytest.raises(FileNotFoundError):
        vol.remove_file("/nonexistent", recursive=True)


def test_scoped_volume_listdir(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    entries = scoped.listdir("agents")
    paths = [e.path for e in entries]
    assert "agents/a1.json" in paths
    assert "agents/a2.json" in paths
    for entry in entries:
        data = scoped.read_file(entry.path)
        assert len(data) > 0


def test_scoped_volume_listdir_preserves_file_type(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    entries = scoped.listdir("agents")
    for entry in entries:
        assert entry.file_type == VolumeFileType.FILE


def test_scoped_volume_chained_scoping(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host").scoped("agents")
    assert scoped.read_file("a1.json") == b'{"id": "a1"}'


def test_scoped_volume_read_nonexistent_raises(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    with pytest.raises(FileNotFoundError):
        scoped.read_file("nonexistent.txt")


def test_scoped_volume_prefix_trailing_slash_stripped() -> None:
    vol = InMemoryVolume(files={"/data/file.txt": b"content"})
    scoped = ScopedVolume(delegate=vol, prefix="/data/")
    assert scoped.read_file("file.txt") == b"content"


# =============================================================================
# VolumeFile tests
# =============================================================================


def test_volume_file_fields() -> None:
    vf = VolumeFile(path="/test.txt", file_type=VolumeFileType.FILE, mtime=1000, size=42)
    assert vf.path == "/test.txt"
    assert vf.file_type == VolumeFileType.FILE
    assert vf.mtime == 1000
    assert vf.size == 42


def test_volume_file_type_enum_values() -> None:
    assert VolumeFileType.FILE == "FILE"
    assert VolumeFileType.DIRECTORY == "DIRECTORY"


# =============================================================================
# HostVolume tests
# =============================================================================


def test_host_volume_get_agent_volume_returns_scoped_volume() -> None:
    agent_id = AgentId.generate()
    vol = InMemoryVolume(files={f"agents/{agent_id}/data.json": b'{"id": "test"}'})
    host_volume = HostVolume(volume=vol)
    agent_volume = host_volume.get_agent_volume(agent_id)
    assert isinstance(agent_volume, ScopedVolume)
    assert agent_volume.read_file("data.json") == b'{"id": "test"}'


def test_host_volume_get_agent_volume_isolates_agents() -> None:
    agent_id_a = AgentId.generate()
    agent_id_b = AgentId.generate()
    vol = InMemoryVolume(
        files={
            f"agents/{agent_id_a}/file.txt": b"agent-a",
            f"agents/{agent_id_b}/file.txt": b"agent-b",
        }
    )
    host_volume = HostVolume(volume=vol)

    vol_a = host_volume.get_agent_volume(agent_id_a)
    vol_b = host_volume.get_agent_volume(agent_id_b)

    assert vol_a.read_file("file.txt") == b"agent-a"
    assert vol_b.read_file("file.txt") == b"agent-b"


def test_host_volume_get_agent_volume_write_goes_to_correct_path() -> None:
    agent_id = AgentId.generate()
    vol = InMemoryVolume(files={})
    host_volume = HostVolume(volume=vol)
    agent_volume = host_volume.get_agent_volume(agent_id)
    agent_volume.write_files({"logs/claude_transcript/events.jsonl": b"line1\nline2\n"})
    assert vol.files[f"agents/{agent_id}/logs/claude_transcript/events.jsonl"] == b"line1\nline2\n"
