from imbue.imbue_common.testing import assert_model_is_frozen
from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType


def test_file_entry_is_immutable() -> None:
    assert_model_is_frozen(FileEntry)


def test_file_entry_optional_fields_default_to_none_when_omitted() -> None:
    entry = FileEntry(name="dir", path="/home/user/dir", file_type=FileType.DIRECTORY)
    assert entry.size is None
    assert entry.modified is None
    assert entry.permissions is None
