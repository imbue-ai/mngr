import pytest
from inline_snapshot import snapshot
from pydantic import ValidationError

from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType
from imbue.mngr_file.data_types import PathRelativeTo


def test_path_relative_to_serialized_values_are_stable() -> None:
    # These string values are the on-the-wire form used by --relative-to parsing
    # and must stay stable; pin the full mapping so any rename/add/remove is caught.
    assert {member.name: member.value for member in PathRelativeTo} == snapshot(
        {"WORK": "WORK", "STATE": "STATE", "HOST": "HOST"}
    )


def test_file_type_serialized_values_are_stable() -> None:
    # FileType values back the stat-char mapping in cli/list.py and the JSON
    # output; pin the full mapping so any rename/add/remove is caught.
    assert {member.name: member.value for member in FileType} == snapshot(
        {
            "FILE": "FILE",
            "DIRECTORY": "DIRECTORY",
            "SYMLINK": "SYMLINK",
            "PIPE": "PIPE",
            "SOCKET": "SOCKET",
            "BLOCK": "BLOCK",
            "CHARACTER": "CHARACTER",
            "OTHER": "OTHER",
        }
    )


def test_file_entry_is_immutable() -> None:
    entry = FileEntry(name="config.toml", path="/home/user/config.toml", file_type=FileType.FILE)
    with pytest.raises(ValidationError):
        entry.name = "other.toml"


def test_file_entry_optional_fields_default_to_none_when_omitted() -> None:
    entry = FileEntry(name="dir", path="/home/user/dir", file_type=FileType.DIRECTORY)
    assert entry.size is None
    assert entry.modified is None
    assert entry.permissions is None
