"""Shared helpers for the codex lifecycle-hook resource tests.

The four lifecycle hooks source the shared ``codex_marker_state.sh`` helper from
``$MNGR_AGENT_STATE_DIR/commands/``, so a test must copy the script under test
(and the helper, and any other hooks it drives) into a temp ``commands/`` dir and
point ``MNGR_AGENT_STATE_DIR`` at the temp state root before running. These
helpers do that provisioning and invoke the hooks via subprocess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from imbue.mngr_codex.codex_config import MARKER_STATE_LIB_SCRIPT_NAME

_RESOURCES_DIR = Path(__file__).parent


def provision_commands_dir(state_dir: Path, script_names: Sequence[str]) -> Path:
    """Copy the named hook scripts plus the shared helper into ``state_dir/commands/``.

    The shared ``codex_marker_state.sh`` helper is always copied (every hook
    sources it). Returns the commands dir so callers can run the scripts from it.
    """
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    names_to_copy = set(script_names) | {MARKER_STATE_LIB_SCRIPT_NAME}
    for script_name in names_to_copy:
        shutil.copy(_RESOURCES_DIR / script_name, commands_dir / script_name)
    return commands_dir


def install_common_transcript_flush_stub(state_dir: Path, sentinel: Path) -> None:
    """Provision a stub ``mngr_common_transcript_lib.sh`` whose
    ``mngr_common_transcript_flush`` just touches ``sentinel``.

    Lets a turn-end hook test observe whether the flush ran without standing up
    the real stream/convert pipeline. Write it after ``provision_commands_dir``
    (which does not copy the shared lib) so it is the only definition.
    """
    (state_dir / "commands" / "mngr_common_transcript_lib.sh").write_text(
        f'mngr_common_transcript_flush() {{ touch "{sentinel}"; }}\n'
    )


def run_codex_hook(
    state_dir: Path,
    script_name: str,
    payload: str,
    is_check_enabled: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Invoke a provisioned hook script from ``state_dir/commands/`` with ``payload`` on stdin.

    Runs with the ambient tmux environment stripped (no ``TMUX``/``TMUX_TMPDIR``),
    so set_active_marker.sh's submit-channel signal takes its headless ``$TMUX``-unset
    path and never invokes ``tmux`` -- keeping these unit tests off the developer's
    real tmux server (and out of the tmux resource guard).
    """
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_TMPDIR")}
    env["MNGR_AGENT_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        ["bash", str(state_dir / "commands" / script_name)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        check=is_check_enabled,
    )
