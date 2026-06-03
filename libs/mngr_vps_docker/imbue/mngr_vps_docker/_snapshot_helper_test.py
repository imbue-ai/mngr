"""Unit tests for the snapshot_helper.* resources and their loader."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_vps_docker.container_setup import load_resource_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_snapshot_helper_script_passes_bash_syntax_check(tmp_path: Path) -> None:
    """`bash -n` must accept the bundled snapshot_helper.sh as valid syntax.

    Also exercises the wheel's resource bundling end-to-end: if the
    pyproject.toml `include` directive ever drops the .sh file, `load_resource_text`
    raises before we even get to the bash check.
    """
    script_path = tmp_path / "snapshot_helper.sh"
    script_path.write_text(load_resource_text("snapshot_helper.sh"))
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_snapshot_helper_unit_is_loadable() -> None:
    """The bundled systemd unit is loadable (pyproject `include` smoke test).

    Combined with the bash-syntax test above, this ensures both resource
    files survive the wheel build.
    """
    load_resource_text("snapshot_helper.service")
