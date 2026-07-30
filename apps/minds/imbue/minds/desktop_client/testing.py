"""Shared non-fixture test helpers for desktop_client tests."""

import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger as loguru_logger

from imbue.minds.desktop_client.restic_cli import _get_restic_binary


def is_workspace_options_pane_hidden(html: str, pane: str) -> bool:
    """Whether the workspace options panel ships ``pane`` hidden (it must ship both).

    Reads the ``hidden`` class off the pane rather than matching its whole class
    attribute, which also carries the layout that lets the pane pin its title
    and nav and scroll its right side. Explodes if the pane is not in the HTML
    at all, so a test cannot pass by asserting a missing pane is not shown.
    """
    match = re.search(rf'data-wsopt-panel="{re.escape(pane)}" class="([^"]*)"', html)
    assert match is not None, f"no {pane!r} pane in the rendered options panel"
    return "hidden" in match.group(1).split()


def workspace_options_pane_html(html: str, pane: str) -> str:
    """The markup of one pane of the workspace options panel, for asserting on its layout.

    The panel ships both panes, so a naive substring search cannot tell which
    one it matched. This slices from the pane's own element to the start of the
    next pane (or the end), which is enough because the two are siblings.
    """
    start = html.find(f'data-wsopt-panel="{pane}"')
    assert start != -1, f"no {pane!r} pane in the rendered options panel"
    next_pane = html.find("data-wsopt-panel=", start + 1)
    return html[start:] if next_pane == -1 else html[start:next_pane]


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
