"""Shared non-fixture test helpers for desktop_client tests."""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger as loguru_logger

from imbue.minds.desktop_client.restic_cli import _get_restic_binary


@contextmanager
def capture_error_logs() -> Iterator[list[str]]:
    """Capture loguru ERROR-level records (a loguru sink; caplog can't hook loguru).

    Every RESTART_FAILED transition must reach error reporting (Principle 3:
    the recovery surface is quiet), so the restart-failure tests assert exactly
    one error record per attempt through this capture.
    """
    records: list[str] = []
    sink_id = loguru_logger.add(lambda msg: records.append(str(msg)), level="ERROR")
    try:
        yield records
    finally:
        loguru_logger.remove(sink_id)


def restic_backup_a_file(repository: str, password: str, source: Path) -> None:
    """Create one snapshot in ``repository`` from ``source`` using plain restic."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        [_get_restic_binary(), "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
