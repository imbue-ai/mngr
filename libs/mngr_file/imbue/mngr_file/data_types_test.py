from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType
from imbue.mngr_file.data_types import PathRelativeTo


def test_path_relative_to_values() -> None:
    assert PathRelativeTo.WORK.value == "WORK"
    assert PathRelativeTo.STATE.value == "STATE"
    assert PathRelativeTo.HOST.value == "HOST"


def test_file_type_values() -> None:
    assert FileType.FILE.value == "FILE"
    assert FileType.DIRECTORY.value == "DIRECTORY"
    assert FileType.SYMLINK.value == "SYMLINK"
    assert FileType.OTHER.value == "OTHER"


def test_file_entry_with_all_fields() -> None:
    entry = FileEntry(
        name="config.toml",
        path="/home/user/config.toml",
        file_type=FileType.FILE,
        size=256,
        modified="2026-03-21T12:00:00+00:00",
        permissions="-rw-r--r--",
    )
    assert entry.name == "config.toml"
    assert entry.size == 256
    assert entry.file_type == FileType.FILE


def test_file_entry_with_optional_fields_none() -> None:
    entry = FileEntry(
        name="dir",
        path="/home/user/dir",
        file_type=FileType.DIRECTORY,
        size=None,
        modified=None,
        permissions=None,
    )
    assert entry.size is None
    assert entry.modified is None
    assert entry.permissions is None
