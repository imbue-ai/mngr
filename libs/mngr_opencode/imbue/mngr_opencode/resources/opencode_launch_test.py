import os
import subprocess
from pathlib import Path
from typing import Final

from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import running_in_own_process_group
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_opencode.opencode_config import READY_SENTINEL_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_PORT_FILENAME

_LAUNCH_SCRIPT: Final[Path] = Path(__file__).parent / LAUNCH_SCRIPT_NAME
_ATTACH_TIMEOUT_SECONDS: Final[float] = 10.0

# Records its subcommand and the state dir's entries at its start; as the server it reports a port and waits to be killed.
_STANDIN_OPENCODE_SCRIPT: Final[str] = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\t%s\\n' "$1" "$(ls -A "$MNGR_AGENT_STATE_DIR" | tr '\\n' ' ')" >> "$STANDIN_OPENCODE_RECORD"
if [ "$1" = serve ]; then
    echo "listening on http://127.0.0.1:47213"
    exec sleep 947213
fi
"""


def _read_state_dir_entries_by_subcommand(record_path: Path) -> dict[str, list[str]]:
    if not record_path.exists():
        return {}
    return {
        subcommand: sorted(entries.split())
        for subcommand, entries in (line.split("\t") for line in record_path.read_text().splitlines())
    }


def test_launch_clears_the_turn_marker_and_readiness_sentinel_a_previous_run_left_before_the_server_starts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ROOT_SESSION_FILENAME).write_text("ses_51834")
    (state_dir / ACTIVE_MARKER_FILENAME).touch()
    (state_dir / READY_SENTINEL_FILENAME).touch()
    standin_opencode_path = tmp_path / "bin" / "opencode"
    standin_opencode_path.parent.mkdir()
    write_executable_script(standin_opencode_path, _STANDIN_OPENCODE_SCRIPT)
    record_path = tmp_path / "standin_record"

    # The script's liveness watcher signals its whole process group, so it runs in a group of its own.
    with running_in_own_process_group(
        command=["bash", str(_LAUNCH_SCRIPT)],
        env={
            "PATH": os.environ["PATH"],
            "MNGR_AGENT_STATE_DIR": str(state_dir),
            "MNGR_OPENCODE_BIN": str(standin_opencode_path),
            "MNGR_OPENCODE_PORT": "0",
            "MNGR_OPENCODE_WORKDIR": "/work-51834",
            "TMPDIR": str(tmp_path / "tmp"),
            "STANDIN_OPENCODE_RECORD": str(record_path),
        },
        stdout=subprocess.DEVNULL,
    ):
        wait_for(
            lambda: "attach" in _read_state_dir_entries_by_subcommand(record_path),
            timeout=_ATTACH_TIMEOUT_SECONDS,
            error_message="the launch script never started the attach client",
        )

    assert _read_state_dir_entries_by_subcommand(record_path) == {
        "serve": ["logs", ROOT_SESSION_FILENAME],
        "attach": ["logs", READY_SENTINEL_FILENAME, ROOT_SESSION_FILENAME, SERVER_PORT_FILENAME],
    }
    assert not (state_dir / ACTIVE_MARKER_FILENAME).exists()
