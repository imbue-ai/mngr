import json
from pathlib import Path

import pytest

from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng.providers.local.volume import LocalVolume
from imbue.mng_file.cli.list import _emit_list_result
from imbue.mng_file.cli.list import _entry_to_field_mapping
from imbue.mng_file.cli.list import _entry_to_json_dict
from imbue.mng_file.cli.list import _get_field_value
from imbue.mng_file.cli.list import list_files_on_volume
from imbue.mng_file.cli.list import parse_find_output
from imbue.mng_file.data_types import FileEntry
from imbue.mng_file.data_types import FileType


def test_parse_find_output_parses_file_entry() -> None:
    output = "myfile.txt\t1024\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/myfile.txt\n"
    entries = parse_find_output(output)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "myfile.txt"
    assert entry.path == "/home/user/myfile.txt"
    assert entry.file_type == FileType.FILE
    assert entry.size == 1024
    assert entry.modified == "2026-03-21+12:00:00"
    assert entry.permissions == "-rw-r--r--"


def test_parse_find_output_parses_directory_entry() -> None:
    output = "subdir\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user/subdir\n"
    entries = parse_find_output(output)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "subdir"
    assert entry.file_type == FileType.DIRECTORY
    assert entry.size is None


def test_parse_find_output_skips_dot_entry() -> None:
    output = ".\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user\n"
    entries = parse_find_output(output)

    assert len(entries) == 0


def test_parse_find_output_parses_multiple_entries() -> None:
    output = (
        "file1.txt\t100\t2026-03-21+12:00:00\tf\t-rw-r--r--\t/home/user/file1.txt\n"
        "file2.txt\t200\t2026-03-21+13:00:00\tf\t-rw-r--r--\t/home/user/file2.txt\n"
        "subdir\t4096\t2026-03-21+10:00:00\td\tdrwxr-xr-x\t/home/user/subdir\n"
    )
    entries = parse_find_output(output)

    assert len(entries) == 3
    assert entries[0].name == "file1.txt"
    assert entries[1].name == "file2.txt"
    assert entries[2].name == "subdir"


def test_parse_find_output_handles_empty_output() -> None:
    entries = parse_find_output("")

    assert len(entries) == 0


def test_parse_find_output_skips_malformed_lines() -> None:
    output = "this is not valid find output\n"
    entries = parse_find_output(output)

    assert len(entries) == 0


def test_parse_find_output_handles_symlink() -> None:
    output = "link.txt\t10\t2026-03-21+12:00:00\tl\tlrwxrwxrwx\t/home/user/link.txt\n"
    entries = parse_find_output(output)

    assert len(entries) == 1
    assert entries[0].file_type == FileType.SYMLINK
    assert entries[0].size == 10


def test_get_field_value_formats_size_with_units() -> None:
    entry = FileEntry(
        name="big.bin",
        path="/big.bin",
        file_type=FileType.FILE,
        size=2048,
        modified=None,
        permissions=None,
    )
    assert _get_field_value(entry, "size") == "2.0 KB"


def test_get_field_value_returns_dash_for_none_size() -> None:
    entry = FileEntry(
        name="dir",
        path="/dir",
        file_type=FileType.DIRECTORY,
        size=None,
        modified=None,
        permissions=None,
    )
    assert _get_field_value(entry, "size") == "-"


def test_get_field_value_returns_dash_for_none_modified() -> None:
    entry = FileEntry(
        name="f",
        path="/f",
        file_type=FileType.FILE,
        size=0,
        modified=None,
        permissions=None,
    )
    assert _get_field_value(entry, "modified") == "-"


def test_get_field_value_returns_empty_for_unknown_field() -> None:
    entry = FileEntry(
        name="f",
        path="/f",
        file_type=FileType.FILE,
        size=0,
        modified=None,
        permissions=None,
    )
    assert _get_field_value(entry, "nonexistent") == ""


def test_get_field_value_returns_name() -> None:
    entry = FileEntry(
        name="test.txt",
        path="/test.txt",
        file_type=FileType.FILE,
        size=100,
        modified="2026-01-01",
        permissions="-rw-r--r--",
    )
    assert _get_field_value(entry, "name") == "test.txt"


def test_get_field_value_returns_path() -> None:
    entry = FileEntry(
        name="test.txt", path="/home/test.txt", file_type=FileType.FILE, size=100, modified=None, permissions=None
    )
    assert _get_field_value(entry, "path") == "/home/test.txt"


def test_get_field_value_returns_file_type() -> None:
    entry = FileEntry(name="d", path="/d", file_type=FileType.DIRECTORY, size=None, modified=None, permissions=None)
    assert _get_field_value(entry, "file_type") == "directory"


def test_get_field_value_returns_permissions() -> None:
    entry = FileEntry(name="f", path="/f", file_type=FileType.FILE, size=0, modified=None, permissions="-rwxr-xr-x")
    assert _get_field_value(entry, "permissions") == "-rwxr-xr-x"


def test_get_field_value_returns_dash_for_none_permissions() -> None:
    entry = FileEntry(name="f", path="/f", file_type=FileType.FILE, size=0, modified=None, permissions=None)
    assert _get_field_value(entry, "permissions") == "-"


def test_get_field_value_returns_modified() -> None:
    entry = FileEntry(
        name="f", path="/f", file_type=FileType.FILE, size=0, modified="2026-03-21+12:00:00", permissions=None
    )
    assert _get_field_value(entry, "modified") == "2026-03-21+12:00:00"


def test_entry_to_field_mapping_returns_correct_mapping() -> None:
    entry = FileEntry(
        name="test.txt",
        path="/test.txt",
        file_type=FileType.FILE,
        size=1024,
        modified="2026-01-01",
        permissions="-rw-r--r--",
    )
    mapping = _entry_to_field_mapping(entry, ("name", "size"))
    assert mapping == {"name": "test.txt", "size": "1.0 KB"}


def test_entry_to_json_dict_includes_all_fields() -> None:
    entry = FileEntry(
        name="test.txt",
        path="/test.txt",
        file_type=FileType.FILE,
        size=1024,
        modified="2026-01-01",
        permissions="-rw-r--r--",
    )
    result = _entry_to_json_dict(entry)
    assert result["name"] == "test.txt"
    assert result["path"] == "/test.txt"
    assert result["file_type"] == "file"
    assert result["size"] == 1024
    assert result["modified"] == "2026-01-01"
    assert result["permissions"] == "-rw-r--r--"


def test_emit_list_result_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)
    _emit_list_result([], ("name",), output_opts)
    captured = capsys.readouterr()
    assert "(empty)" in captured.out


def test_emit_list_result_human_with_entries(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)
    entries = [
        FileEntry(
            name="file.txt",
            path="/file.txt",
            file_type=FileType.FILE,
            size=100,
            modified="2026-01-01",
            permissions=None,
        ),
    ]
    _emit_list_result(entries, ("name", "file_type", "size"), output_opts)
    captured = capsys.readouterr()
    assert "file.txt" in captured.out
    assert "file" in captured.out


def test_emit_list_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON, format_template=None)
    entries = [
        FileEntry(name="a.txt", path="/a.txt", file_type=FileType.FILE, size=50, modified=None, permissions=None),
    ]
    _emit_list_result(entries, ("name",), output_opts)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["count"] == 1
    assert len(parsed["files"]) == 1
    assert parsed["files"][0]["name"] == "a.txt"


def test_emit_list_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSONL, format_template=None)
    entries = [
        FileEntry(name="a.txt", path="/a.txt", file_type=FileType.FILE, size=50, modified=None, permissions=None),
        FileEntry(name="b.txt", path="/b.txt", file_type=FileType.FILE, size=100, modified=None, permissions=None),
    ]
    _emit_list_result(entries, ("name",), output_opts)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "a.txt"
    assert json.loads(lines[1])["name"] == "b.txt"


def test_list_files_on_volume_returns_file_entries(tmp_path: Path) -> None:
    # Create files in the temp directory
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
    entries = list_files_on_volume(volume=volume, vol_path=".", is_recursive=False)

    assert entries == []


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
