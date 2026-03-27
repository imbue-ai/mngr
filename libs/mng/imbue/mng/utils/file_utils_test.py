import os
import stat
from pathlib import Path

import pytest

from imbue.mng.utils.file_utils import atomic_write


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"

    atomic_write(target, "hello")

    assert target.read_text() == "hello"


def test_atomic_write_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"
    target.write_text("old content")

    atomic_write(target, "new content")

    assert target.read_text() == "new content"


def test_atomic_write_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "output.txt"

    atomic_write(target, "nested")

    assert target.read_text() == "nested"


def test_atomic_write_preserves_existing_permissions(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"
    target.write_text("original")
    os.chmod(target, 0o644)

    atomic_write(target, "updated")

    actual_mode = stat.S_IMODE(target.stat().st_mode)
    assert actual_mode == 0o644


def test_atomic_write_preserves_readonly_permissions(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"
    target.write_text("original")
    os.chmod(target, 0o444)

    atomic_write(target, "updated")

    actual_mode = stat.S_IMODE(target.stat().st_mode)
    assert actual_mode == 0o444


def test_atomic_write_new_file_gets_default_permissions(tmp_path: Path) -> None:
    target = tmp_path / "new_file.txt"

    atomic_write(target, "content")

    actual_mode = stat.S_IMODE(target.stat().st_mode)
    assert actual_mode == 0o600


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses permission checks")
def test_atomic_write_raises_on_unwritable_directory(tmp_path: Path) -> None:
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    os.chmod(locked_dir, 0o555)

    target = locked_dir / "subdir" / "output.txt"
    try:
        with pytest.raises(OSError):
            atomic_write(target, "content")
    finally:
        os.chmod(locked_dir, 0o755)


def test_atomic_write_cleans_up_temp_file_on_replace_failure(tmp_path: Path) -> None:
    """When os.replace fails, atomic_write should remove the temp file and re-raise.

    Creates a directory at the target path, so os.replace fails with
    IsADirectoryError. The temp file should be cleaned up.
    """
    target = tmp_path / "output.txt"
    # Place a non-empty directory at the target path so os.replace fails
    target.mkdir()
    (target / "blocker").write_text("prevents replace")

    with pytest.raises(OSError):
        atomic_write(target, "content")

    # The directory should still exist (replace failed)
    assert target.is_dir()

    # No leftover .tmp files should remain in the parent directory
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_atomic_write_to_new_nested_directory(tmp_path: Path) -> None:
    """atomic_write should create deeply nested parent directories that don't exist."""
    target = tmp_path / "x" / "y" / "z" / "deeply" / "nested" / "file.txt"

    atomic_write(target, "deep content")

    assert target.exists()
    assert target.read_text() == "deep content"
    assert target.parent.is_dir()
