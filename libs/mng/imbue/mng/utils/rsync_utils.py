import re

from imbue.imbue_common.pure import pure

# Patterns for --stats output lines
_FILES_TRANSFERRED_RE = re.compile(r"Number of files transferred:\s+(\d+)")
_TOTAL_TRANSFERRED_SIZE_RE = re.compile(r"Total transferred file size:\s+([\d,]+)")


@pure
def parse_rsync_output(
    # stdout from rsync command (must be run with --stats)
    output: str,
    # Tuple of (files_transferred, bytes_transferred)
) -> tuple[int, int]:
    """Parse rsync --stats output to extract transfer statistics.

    Parses the structured stats block from rsync to extract:
    - Number of files transferred
    - Total transferred file size in bytes

    Returns a tuple of (files_transferred, bytes_transferred).
    """
    files_transferred = 0
    bytes_transferred = 0

    for line in output.split("\n"):
        line = line.strip()

        match = _FILES_TRANSFERRED_RE.match(line)
        if match:
            files_transferred = int(match.group(1))
            continue

        match = _TOTAL_TRANSFERRED_SIZE_RE.match(line)
        if match:
            bytes_transferred = int(match.group(1).replace(",", ""))
            continue

    return files_transferred, bytes_transferred
