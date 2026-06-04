"""Unit tests for rsync_utils module."""

import pytest

from imbue.mngr.utils.rsync_utils import parse_rsync_output


@pytest.mark.parametrize(
    ("total_size", "transferred_size", "expected_files", "expected_bytes"),
    [
        ("5,678 B", "1,234 B", 3, 1234),
        ("1,000 B", "0 B", 0, 0),
        ("10,000 B", "345 B", 3, 345),
        ("2,000,000,000 B", "1,234,567,890 B", 1, 1234567890),
    ],
    ids=["with_files", "no_files_transferred", "dry_run", "large_numbers"],
)
def test_parse_rsync_output_extracts_stats(
    total_size: str,
    transferred_size: str,
    expected_files: int,
    expected_bytes: int,
) -> None:
    """The parser reads the files-transferred count and the transferred byte total."""
    output = f"""Number of files: 5
Number of files transferred: {expected_files}
Total file size: {total_size}
Total transferred file size: {transferred_size}
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == expected_files
    assert bytes_transferred == expected_bytes


def test_parse_rsync_output_ignores_total_file_size_line() -> None:
    """Only "Total transferred file size" counts; the similar "Total file size" must be ignored.

    The two lines share the "Total ... file size:" shape, so a sloppy parser could
    match "Total file size:" and report bytes that were never transferred. With only
    that line present (no "Total transferred file size:"), bytes_transferred must be 0.
    """
    output = """Number of files: 5
Number of files transferred: 2
Total file size: 9,999 B
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 2
    assert bytes_transferred == 0


def test_parse_rsync_output_with_no_stats_lines() -> None:
    """Test parsing rsync output when stats lines are missing."""
    output = """sending incremental file list
file1.txt
file2.txt
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 0
    assert bytes_transferred == 0


def test_parse_rsync_output_empty_string() -> None:
    """Test parsing empty rsync output."""
    output = ""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 0
    assert bytes_transferred == 0


def test_parse_rsync_output_whitespace_only() -> None:
    """Test parsing rsync output with only whitespace."""
    output = "   \n  \n   "
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 0
    assert bytes_transferred == 0
