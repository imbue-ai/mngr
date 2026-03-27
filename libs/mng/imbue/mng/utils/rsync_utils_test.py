"""Unit tests for rsync_utils module."""

from imbue.mng.utils.rsync_utils import parse_rsync_output


def test_parse_rsync_output_with_files() -> None:
    """Test parsing rsync --stats output with file transfers."""
    output = """Number of files: 5
Number of files transferred: 3
Total file size: 5,678 B
Total transferred file size: 1,234 B
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 3
    assert bytes_transferred == 1234


def test_parse_rsync_output_empty() -> None:
    """Test parsing rsync --stats output with no files transferred."""
    output = """Number of files: 10
Number of files transferred: 0
Total file size: 1,000 B
Total transferred file size: 0 B
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 0
    assert bytes_transferred == 0


def test_parse_rsync_output_dry_run() -> None:
    """Test parsing rsync --stats output in dry run mode."""
    output = """Number of files: 5
Number of files transferred: 3
Total file size: 10,000 B
Total transferred file size: 345 B
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 3
    assert bytes_transferred == 345


def test_parse_rsync_output_large_numbers() -> None:
    """Test parsing rsync --stats output with large byte counts."""
    output = """Number of files: 1
Number of files transferred: 1
Total file size: 2,000,000,000 B
Total transferred file size: 1,234,567,890 B
"""
    files, bytes_transferred = parse_rsync_output(output)
    assert files == 1
    assert bytes_transferred == 1234567890


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
