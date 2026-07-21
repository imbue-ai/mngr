"""Shared non-fixture test helpers for desktop_client tests."""

import json
import os
import re
import subprocess
from pathlib import Path

from imbue.minds.desktop_client.restic_cli import _get_restic_binary


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


def parse_boot_island(html: str) -> dict:
    """Extract and parse a rendered page's boot-state JSON island (``id="minds-boot-state"``)."""
    match = re.search(r'<script type="application/json" id="minds-boot-state">(.*?)</script>', html, re.DOTALL)
    assert match is not None, "page carries no boot-state island"
    return json.loads(match.group(1))
