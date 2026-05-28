"""Unit tests for the snapshot_helper.* resources and their loader."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_vps_docker.instance import OUTER_HELPER_SCRIPT_PATH
from imbue.mngr_vps_docker.instance import OUTER_HELPER_SERVICE_PATH
from imbue.mngr_vps_docker.instance import _load_resource_text


def test_load_resource_text_returns_snapshot_helper_script() -> None:
    """The bundled snapshot_helper.sh must be loadable via importlib.resources."""
    content = _load_resource_text("snapshot_helper.sh")
    assert content.startswith("#!/usr/bin/env bash")
    assert "snapshot" in content
    assert "cleanup" in content


def test_load_resource_text_returns_snapshot_helper_service() -> None:
    """The bundled systemd unit must be loadable and contain a [Service] section."""
    content = _load_resource_text("snapshot_helper.service")
    assert "[Unit]" in content
    assert "[Service]" in content
    assert "ExecStart=/usr/local/sbin/snapshot_helper.sh" in content
    assert "Restart=always" in content


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_snapshot_helper_script_passes_bash_syntax_check(tmp_path: Path) -> None:
    """`bash -n` must accept snapshot_helper.sh as valid syntax."""
    script_path = tmp_path / "snapshot_helper.sh"
    script_path.write_text(_load_resource_text("snapshot_helper.sh"))
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_paths_are_well_known_absolute_paths() -> None:
    """The outer install paths must be absolute (we use them in shell commands)."""
    assert OUTER_HELPER_SCRIPT_PATH.is_absolute()
    assert OUTER_HELPER_SERVICE_PATH.is_absolute()
    assert str(OUTER_HELPER_SCRIPT_PATH).startswith("/usr/local/sbin/")
    assert str(OUTER_HELPER_SERVICE_PATH).startswith("/etc/systemd/system/")
