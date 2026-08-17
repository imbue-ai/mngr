"""Shared helpers for the codex resource-script tests.

A test that exercises a provisioned command script (a hook, a background-task helper) copies it
into a temp ``commands/`` dir and points ``MNGR_AGENT_STATE_DIR`` at the temp state root before
running. ``provision_commands_dir`` does that provisioning.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

_RESOURCES_DIR = Path(__file__).parent


def provision_commands_dir(state_dir: Path, script_names: Sequence[str]) -> Path:
    """Copy the named scripts into ``state_dir/commands/`` and return the commands dir."""
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    for script_name in script_names:
        shutil.copy(_RESOURCES_DIR / script_name, commands_dir / script_name)
    return commands_dir
