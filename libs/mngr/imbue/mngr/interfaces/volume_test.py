import pytest

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.mock_volume_test import InMemoryVolume
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.interfaces.volume import ScopedVolume
from imbue.mngr.interfaces.volume import _scoped_path
from imbue.mngr.primitives import AgentId

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
    # Reading each listed entry must resolve to the correct backing file, so
    # assert exact contents (matching volume_with_files) rather than just
    # non-empty bytes -- the latter would still pass if a scoped read returned
    # the wrong file's data.
    assert scoped.read_file("agents/a1.json") == b'{"id": "a1"}'
    assert scoped.read_file("agents/a2.json") == b'{"id": "a2"}'


def test_scoped_volume_listdir_preserves_file_type(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    entries = scoped.listdir("agents")
    for entry in entries:
        assert entry.file_type == FileType.FILE


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


def test_scoped_volume_path_exists_file(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    assert scoped.path_exists("data.json") is True


def test_scoped_volume_path_exists_directory(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    assert scoped.path_exists("agents") is True


def test_scoped_volume_path_exists_missing(volume_with_files: InMemoryVolume) -> None:
    scoped = volume_with_files.scoped("/host")
    assert scoped.path_exists("nonexistent.txt") is False
    assert scoped.path_exists("nonexistent_dir") is False


# =============================================================================
# VolumeFile tests
# =============================================================================


def test_volume_file_size_reflects_listed_file_contents() -> None:
    """listdir derives VolumeFile.size from the actual file contents.

    This ties the VolumeFile fields to the production listing logic rather than
    to literals the test itself passed into the constructor.
    """
    vol = InMemoryVolume(files={"report.txt": b"hello world"})
    (entry,) = vol.listdir("")
    assert entry.path == "report.txt"
    assert entry.file_type == FileType.FILE
    assert entry.size == len(b"hello world")


def test_volume_file_type_enum_values() -> None:
    assert FileType.FILE == "FILE"
    assert FileType.DIRECTORY == "DIRECTORY"


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
