import pytest
from pydantic import ValidationError

from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType


def test_file_entry_is_immutable() -> None:
    entry = FileEntry(name="config.toml", path="/home/user/config.toml", file_type=FileType.FILE)
    with pytest.raises(ValidationError):
        entry.name = "other.toml"


def test_file_entry_optional_fields_default_to_none_when_omitted() -> None:
    entry = FileEntry(name="dir", path="/home/user/dir", file_type=FileType.DIRECTORY)
    assert entry.size is None
    assert entry.modified is None
    assert entry.permissions is None
