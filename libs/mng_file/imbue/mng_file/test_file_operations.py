"""Integration tests for file get/put/list operations on localhost."""

from pathlib import Path

from imbue.mng.config.data_types import MngContext
from imbue.mng_file.cli.list import list_files_on_host
from imbue.mng_file.cli.target import resolve_file_target
from imbue.mng_file.data_types import FileType
from imbue.mng_file.data_types import PathRelativeTo


def test_list_files_on_localhost(temp_mng_ctx: MngContext) -> None:
    """List files on the local host via the host interface."""
    resolved = resolve_file_target(
        target_identifier="localhost",
        mng_ctx=temp_mng_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    entries = list_files_on_host(
        host=resolved.host,
        directory=resolved.base_path,
        is_recursive=False,
    )
    # The host dir should contain at least some standard files/dirs
    names = {e.name for e in entries}
    assert len(names) > 0


def test_put_and_get_file_on_localhost(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """Write a file to the local host dir and read it back."""
    resolved = resolve_file_target(
        target_identifier="localhost",
        mng_ctx=temp_mng_ctx,
        relative_to=PathRelativeTo.HOST,
    )

    # Write a test file
    test_content = b"integration test content 82749"
    test_file_name = "test-file-integration-82749.txt"
    test_path = resolved.base_path / test_file_name
    resolved.host.write_file(test_path, test_content)

    # Read it back
    read_content = resolved.host.read_file(test_path)
    assert read_content == test_content

    # List the directory and verify the file appears
    entries = list_files_on_host(
        host=resolved.host,
        directory=resolved.base_path,
        is_recursive=False,
    )
    names = {e.name for e in entries}
    assert test_file_name in names

    # Verify the file entry has correct attributes
    file_entry = next(e for e in entries if e.name == test_file_name)
    assert file_entry.file_type == FileType.FILE
    assert file_entry.size == len(test_content)

    # Clean up
    test_path.unlink()


def test_list_files_recursive_on_localhost(temp_mng_ctx: MngContext) -> None:
    """List files recursively on the local host dir."""
    resolved = resolve_file_target(
        target_identifier="localhost",
        mng_ctx=temp_mng_ctx,
        relative_to=PathRelativeTo.HOST,
    )

    # Create a nested structure
    nested_dir = resolved.base_path / "test-nested-dir-83921"
    nested_dir.mkdir(exist_ok=True)
    nested_file = nested_dir / "nested-file.txt"
    nested_file.write_text("nested content")

    entries = list_files_on_host(
        host=resolved.host,
        directory=resolved.base_path,
        is_recursive=True,
    )
    names = {e.name for e in entries}
    assert "test-nested-dir-83921" in names
    assert "nested-file.txt" in names

    # Clean up
    nested_file.unlink()
    nested_dir.rmdir()
