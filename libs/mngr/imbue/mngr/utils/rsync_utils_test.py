"""Unit tests for rsync_utils module."""

from imbue.mngr.utils.rsync_utils import parse_rsync_output


def test_parse_rsync_output_with_files() -> None:
    """Test parsing rsync --stats output with file transfers."""
    output = """Number of files: 5
Number of files transferred: 3
Total file size: 5,678 B
Total transferred file size: 1,234 B
"""
    result = parse_rsync_output(output)
    assert result is not None
    files, bytes_transferred = result
    assert files == 3
    assert bytes_transferred == 1234


def test_parse_rsync_output_empty() -> None:
    """Test parsing rsync --stats output with no files transferred."""
    output = """Number of files: 10
Number of files transferred: 0
Total file size: 1,000 B
Total transferred file size: 0 B
"""
    result = parse_rsync_output(output)
    assert result is not None
    files, bytes_transferred = result
    assert files == 0
    assert bytes_transferred == 0


def test_parse_rsync_output_dry_run() -> None:
    """Test parsing rsync --stats output in dry run mode."""
    output = """Number of files: 5
Number of files transferred: 3
Total file size: 10,000 B
Total transferred file size: 345 B
"""
    result = parse_rsync_output(output)
    assert result is not None
    files, bytes_transferred = result
    assert files == 3
    assert bytes_transferred == 345


def test_parse_rsync_output_large_numbers() -> None:
    """Test parsing rsync --stats output with large byte counts."""
    output = """Number of files: 1
Number of files transferred: 1
Total file size: 2,000,000,000 B
Total transferred file size: 1,234,567,890 B
"""
    result = parse_rsync_output(output)
    assert result is not None
    files, bytes_transferred = result
    assert files == 1
    assert bytes_transferred == 1234567890


def test_parse_rsync_output_with_no_stats_lines_returns_none() -> None:
    """Output without a --stats block returns None (unparseable), not a fake (0, 0)."""
    output = """sending incremental file list
file1.txt
file2.txt
"""
    assert parse_rsync_output(output) is None


def test_parse_rsync_output_empty_string_returns_none() -> None:
    """Empty rsync output has no stats block, so it is unparseable (None)."""
    assert parse_rsync_output("") is None


def test_parse_rsync_output_whitespace_only_returns_none() -> None:
    """Whitespace-only output has no stats block, so it is unparseable (None)."""
    assert parse_rsync_output("   \n  \n   ") is None


def test_parse_rsync_output_zero_transfer_block_is_distinct_from_unparseable() -> None:
    """A genuine zero-file transfer still has the stats block and parses to (0, 0), not None."""
    output = """Number of files: 10
Number of files transferred: 0
Total transferred file size: 0 B
"""
    result = parse_rsync_output(output)
    assert result == (0, 0)
