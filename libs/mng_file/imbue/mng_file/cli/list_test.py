import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng.providers.local.volume import LocalVolume
from imbue.mng_file.cli.list import _emit_list_result
from imbue.mng_file.cli.list import _entry_to_field_mapping
from imbue.mng_file.cli.list import _entry_to_json_dict
from imbue.mng_file.cli.list import _get_field_value
from imbue.mng_file.cli.list import list_files_on_volume
from imbue.mng_file.cli.list import parse_list_output
from imbue.mng_file.data_types import FileEntry
from imbue.mng_file.data_types import FileType


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


# --- parse_list_output ---


def test_parse_list_output_parses_file_entry() -> None:
    output = "myfile.txt\t1024\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/myfile.txt\n"
    entries = parse_list_output(output)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "myfile.txt"
    assert entry.path == "/home/user/myfile.txt"
    assert entry.file_type == FileType.FILE
    assert entry.size == 1024
    assert entry.modified == "2026-03-21+12:00:00"
    assert entry.permissions == "-rw-r--r--"


def test_parse_list_output_parses_directory_with_none_size() -> None:
    output = "subdir\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user/subdir\n"
    entries = parse_list_output(output)

    assert len(entries) == 1
    assert entries[0].file_type == FileType.DIRECTORY
    assert entries[0].size is None


def test_parse_list_output_skips_dot_entry() -> None:
    output = ".\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user\n"
    assert parse_list_output(output) == []


def test_parse_list_output_parses_multiple_entries() -> None:
    output = (
        "file1.txt\t100\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/file1.txt\n"
        "file2.txt\t200\t2026-03-21+13:00:00\tf\t-rw-r--r--\t/home/user/file2.txt\n"
        "subdir\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user/subdir\n"
    )
    entries = parse_list_output(output)
    assert [e.name for e in entries] == ["file1.txt", "file2.txt", "subdir"]


def test_parse_list_output_handles_empty_output() -> None:
    assert parse_list_output("") == []


def test_parse_list_output_skips_malformed_lines() -> None:
    assert parse_list_output("this is not valid find output\n") == []


def test_parse_list_output_handles_non_numeric_size() -> None:
    output = "file.txt\tnotanumber\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/file.txt\n"
    entries = parse_list_output(output)

    assert len(entries) == 1
    assert entries[0].size is None


def test_parse_list_output_skips_empty_name() -> None:
    output = "\t100\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/\n"
    entries = parse_list_output(output)

    assert len(entries) == 0


def test_parse_list_output_handles_symlink() -> None:
    output = "link.txt\t10\t2026-03-21+12:00:00\tl\tlrwxrwxrwx\t/home/user/link.txt\n"
    entries = parse_list_output(output)

    assert len(entries) == 1
    assert entries[0].file_type == FileType.SYMLINK
    assert entries[0].size == 10


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


# --- list_files_on_volume ---


def test_list_files_on_volume_returns_file_entries(tmp_path: Path) -> None:
    (tmp_path / "file1.txt").write_text("hello")
    (tmp_path / "file2.bin").write_bytes(b"\x00" * 100)
    (tmp_path / "subdir").mkdir()

    volume = LocalVolume(root_path=tmp_path)
    entries = list_files_on_volume(volume=volume, vol_path=".", is_recursive=False)

    names = {e.name for e in entries}
    assert "file1.txt" in names
    assert "file2.bin" in names
    assert "subdir" in names

    file_entry = next(e for e in entries if e.name == "file1.txt")
    assert file_entry.file_type == FileType.FILE
    assert file_entry.size == 5

    dir_entry = next(e for e in entries if e.name == "subdir")
    assert dir_entry.file_type == FileType.DIRECTORY
    assert dir_entry.size is None


def test_list_files_on_volume_empty_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    volume = LocalVolume(root_path=empty_dir)
    assert list_files_on_volume(volume=volume, vol_path=".", is_recursive=False) == []


def test_list_files_on_volume_recursive(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "top.txt").write_text("top")
    sub = root / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")
    deep = sub / "deep"
    deep.mkdir()
    (deep / "deep.txt").write_text("deep")

    volume = LocalVolume(root_path=root)
    entries = list_files_on_volume(volume=volume, vol_path=".", is_recursive=True)

    names = {e.name for e in entries}
    assert "top.txt" in names
    assert "subdir" in names
    assert "nested.txt" in names
    assert "deep" in names
    assert "deep.txt" in names
