"""Resolve the default mngr host directory from environment variables.

Stdlib-only so it can be imported from lightweight/fast-path modules
(e.g. the tab-completion entrypoint) without pulling in third-party deps.
"""

import os
from pathlib import Path


def read_default_host_dir() -> Path:
    """Return the default host directory derived from environment variables.

    Resolves MNGR_HOST_DIR (explicit override) or falls back to ~/.{MNGR_ROOT_NAME}
    (default: ~/.mngr).
    """
    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    env_host_dir = os.environ.get("MNGR_HOST_DIR")
    base_dir = Path(env_host_dir) if env_host_dir else Path(f"~/.{root_name}")
    return base_dir.expanduser()
