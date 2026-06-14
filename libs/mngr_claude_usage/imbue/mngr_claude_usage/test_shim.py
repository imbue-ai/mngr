"""Integration tests for claude_statusline.sh.

Exercises the provisioned shim shell script directly via subprocess. This is a
``test_*.py`` integration file because it spawns ``bash`` against the real shell
artifact rather than testing Python in isolation, matching how the sibling writer
script is tested in test_writer.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr_claude_usage.plugin import _provision_statusline_shim
from imbue.mngr_claude_usage.plugin import _stable_shim_path

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed; required by claude_statusline.sh"
)


def test_shim_exits_zero_when_mngr_agent_state_dir_unset(local_host: Host, tmp_path: Path) -> None:
    """Standalone claude invocation: no MNGR_AGENT_STATE_DIR in env. The shim
    must exit 0 and emit nothing -- claude renders the statusline every couple
    of seconds and a non-zero exit would surface as a visible error."""
    state_dir = local_host.host_dir / "agents" / "agent-noenv"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _provision_statusline_shim(local_host, state_dir, work_dir)

    shim_path = _stable_shim_path(local_host.host_dir)
    env = {k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"}
    result = subprocess.run(
        ["bash", str(shim_path)],
        input=b'{"session_id":"abc"}',
        capture_output=True,
        env=env,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b""
    assert result.stderr == b""
