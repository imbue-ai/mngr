import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr_file.cli.list import _emit_list_result
from imbue.mngr_file.cli.list import _entry_to_field_mapping
from imbue.mngr_file.cli.list import _entry_to_json_dict
from imbue.mngr_file.cli.list import _get_field_value
from imbue.mngr_file.cli.list import _volume_file_to_entry
from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType

_HOST_ID = "host-00000000000000000000000000000001"


def _make_file_entry(
    name: str = "f",
    path: str = "/f",
    file_type: FileType = FileType.FILE,
    size: int | None = 0,
    modified: str | None = None,
    permissions: str | None = None,
) -> FileEntry:
    return FileEntry(
        name=name,
        path=path,
        file_type=file_type,
        size=size,
        modified=modified,
        permissions=permissions,
    )


# --- _volume_file_to_entry ---


def test_volume_file_to_entry_file() -> None:
    vf = VolumeFile(
        path="/home/user/myfile.txt",
        file_type=FileType.FILE,
        mtime=1742558400,
        size=1024,
    )
    entry = _volume_file_to_entry(vf)
    assert entry.name == "myfile.txt"
    assert entry.path == "/home/user/myfile.txt"
    assert entry.file_type == FileType.FILE
    assert entry.size == 1024
    # mtime is rendered as an ISO timestamp in UTC.
    assert entry.modified == datetime.fromtimestamp(1742558400, tz=timezone.utc).isoformat()
    # permissions passes through from the VolumeFile; this one has none set.
    assert entry.permissions is None


def test_volume_file_to_entry_passes_through_type_and_permissions() -> None:
    # A host-produced VolumeFile carries the full type and a mode string; both
    # flow through to the FileEntry unchanged.
    vf = VolumeFile(
        path="/home/user/link",
        file_type=FileType.SYMLINK,
        mtime=1742558400,
        size=12,
        permissions="lrwxr-xr-x",
    )
    entry = _volume_file_to_entry(vf)
    assert entry.file_type == FileType.SYMLINK
    assert entry.permissions == "lrwxr-xr-x"


def test_volume_file_to_entry_directory_has_none_size() -> None:
    vf = VolumeFile(path="/home/user/subdir", file_type=FileType.DIRECTORY, mtime=1742558400, size=4096)
    entry = _volume_file_to_entry(vf)
    assert entry.file_type == FileType.DIRECTORY
    assert entry.size is None


def test_volume_file_to_entry_zero_mtime_yields_none_modified() -> None:
    vf = VolumeFile(path="/home/user/f.txt", file_type=FileType.FILE, mtime=0, size=10)
    entry = _volume_file_to_entry(vf)
    assert entry.modified is None


def test_volume_file_to_entry_basename_of_root_level_path() -> None:
    vf = VolumeFile(path="topfile", file_type=FileType.FILE, mtime=0, size=3)
    entry = _volume_file_to_entry(vf)
    assert entry.name == "topfile"


# --- _get_field_value (parameterized) ---


@pytest.mark.parametrize(
    ("field", "entry_kwargs", "expected"),
    [
        ("name", {"name": "test.txt"}, "test.txt"),
        ("path", {"path": "/home/test.txt"}, "/home/test.txt"),
        ("file_type", {"file_type": FileType.DIRECTORY}, "directory"),
        ("file_type", {"file_type": FileType.SYMLINK}, "symlink"),
        ("size", {"size": 2048}, "2.0 KB"),
        ("size", {"size": None, "file_type": FileType.DIRECTORY}, "-"),
        ("modified", {"modified": "2026-03-21+12:00:00"}, "2026-03-21+12:00:00"),
        ("modified", {"modified": None}, "-"),
        ("permissions", {"permissions": "-rwxr-xr-x"}, "-rwxr-xr-x"),
        ("permissions", {"permissions": None}, "-"),
    ],
    ids=[
        "name",
        "path",
        "file_type_dir",
        "file_type_symlink",
        "size_formatted",
        "size_none",
        "modified_present",
        "modified_none",
        "permissions_present",
        "permissions_none",
    ],
)
def test_get_field_value(field: str, entry_kwargs: dict[str, Any], expected: str) -> None:
    entry = _make_file_entry(**entry_kwargs)
    assert _get_field_value(entry, field) == expected


def test_get_field_value_returns_empty_for_unknown_field() -> None:
    assert _get_field_value(_make_file_entry(), "nonexistent") == ""


# --- _entry_to_field_mapping / _entry_to_json_dict ---


def test_entry_to_field_mapping_returns_correct_mapping() -> None:
    entry = _make_file_entry(name="test.txt", size=1024)
    mapping = _entry_to_field_mapping(entry, ("name", "size"))
    assert mapping == {"name": "test.txt", "size": "1.0 KB"}


def test_entry_to_json_dict_includes_all_fields() -> None:
    entry = _make_file_entry(
        name="test.txt", path="/test.txt", size=1024, modified="2026-01-01", permissions="-rw-r--r--"
    )
    result = _entry_to_json_dict(entry)
    assert result["name"] == "test.txt"
    assert result["path"] == "/test.txt"
    assert result["file_type"] == "file"
    assert result["size"] == 1024
    assert result["modified"] == "2026-01-01"
    assert result["permissions"] == "-rw-r--r--"


# --- _emit_list_result ---


def test_emit_list_result_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)
    _emit_list_result([], ("name",), output_opts)
    assert "(empty)" in capsys.readouterr().out


def test_emit_list_result_human_with_entries(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)
    entries = [_make_file_entry(name="file.txt", size=100)]
    _emit_list_result(entries, ("name", "file_type", "size"), output_opts)
    out = capsys.readouterr().out
    assert "file.txt" in out
    assert "file" in out


def test_emit_list_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON, format_template=None)
    entries = [_make_file_entry(name="a.txt", path="/a.txt", size=50)]
    _emit_list_result(entries, ("name",), output_opts)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["count"] == 1
    assert parsed["files"][0]["name"] == "a.txt"


def test_emit_list_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSONL, format_template=None)
    entries = [
        _make_file_entry(name="a.txt", path="/a.txt", size=50),
        _make_file_entry(name="b.txt", path="/b.txt", size=100),
    ]
    _emit_list_result(entries, ("name",), output_opts)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "a.txt"
    assert json.loads(lines[1])["name"] == "b.txt"


# --- list through a readable offline host ---


def _make_readable_offline_host(
    provider: DockerProviderInstance,
    host_id: HostId,
) -> OfflineHostWithVolume:
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    host = provider._create_host_from_host_record(record)
    assert isinstance(host, OfflineHostWithVolume)
    return host


def _host_volume_root(provider: DockerProviderInstance, host_id: HostId, volume_root: Path) -> Path:
    """Return the on-disk directory backing the host's volume (host_dir root)."""
    vol_id = DockerProviderInstance._volume_id_for_host(host_id)
    root = volume_root / "volumes" / str(vol_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_offline_host_list_directory_returns_entries(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """Listing a directory through a volume-backed offline host yields entries with permissions=None."""
    host_id = HostId(_HOST_ID)
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    host = _make_readable_offline_host(provider, host_id)

    root = _host_volume_root(provider, host_id, tmp_path)
    (root / "file1.txt").write_text("hello")
    (root / "file2.bin").write_bytes(b"\x00" * 100)
    (root / "subdir").mkdir()

    volume_files = host.list_directory(host.host_dir, recursive=False)
    entries = [_volume_file_to_entry(vf) for vf in volume_files]

    by_name = {e.name: e for e in entries}
    assert {"file1.txt", "file2.bin", "subdir"} <= set(by_name)

    assert by_name["file1.txt"].file_type == FileType.FILE
    assert by_name["file1.txt"].size == 5
    assert by_name["subdir"].file_type == FileType.DIRECTORY
    assert by_name["subdir"].size is None

    # Offline listing never reports permissions.
    assert all(e.permissions is None for e in entries)
    # Entries carry absolute paths under host_dir.
    assert by_name["file1.txt"].path == str(host.host_dir / "file1.txt")


def test_offline_host_list_directory_recursive(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    host_id = HostId(_HOST_ID)
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    host = _make_readable_offline_host(provider, host_id)

    root = _host_volume_root(provider, host_id, tmp_path)
    (root / "top.txt").write_text("top")
    sub = root / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")

    volume_files = host.list_directory(host.host_dir, recursive=True)
    names = {_volume_file_to_entry(vf).name for vf in volume_files}
    assert {"top.txt", "subdir", "nested.txt"} <= names


def test_offline_host_read_file_by_absolute_path(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """A volume-backed offline host reads files addressed by absolute host_dir paths."""
    host_id = HostId(_HOST_ID)
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    host = _make_readable_offline_host(provider, host_id)

    root = _host_volume_root(provider, host_id, tmp_path)
    (root / "agents").mkdir()
    (root / "agents" / "state.json").write_text("payload")

    assert host.read_file(host.host_dir / "agents" / "state.json") == b"payload"
