"""Resolve the default mngr host directory from environment variables.

Stdlib-only so it can be imported from lightweight/fast-path modules
(e.g. the tab-completion entrypoint) without pulling in third-party deps.
"""

import os
from pathlib import Path


def read_root_name() -> str:
    """Return the mngr root name from MNGR_ROOT_NAME, defaulting to "mngr".

    The root name determines the convention-based host directory name
    (``~/.{root_name}``); this is the single source for that default.
    """
    return os.environ.get("MNGR_ROOT_NAME", "mngr")


def read_default_host_dir() -> Path:
    """Return the default host directory derived from environment variables.

    Resolves MNGR_HOST_DIR (explicit override) or falls back to ~/.{MNGR_ROOT_NAME}
    (default: ~/.mngr).
    """
    env_host_dir = os.environ.get("MNGR_HOST_DIR")
    base_dir = Path(env_host_dir) if env_host_dir else Path(f"~/.{read_root_name()}")
    return base_dir.expanduser()


def deploy_dest_host_dir() -> Path:
    """Return the home-relative host dir a deployed/remote mngr resolves to.

    This is ``~/.{root_name}`` as a tilde path (deliberately NOT expanded):
    deploy destinations resolve against the *remote/container* ``$HOME``, and a
    deployed mngr with no ``MNGR_HOST_DIR`` set finds its host dir there (see
    ``read_default_host_dir``). Local host-dir files are reparented onto this
    root for deployment, so the mapping stays correct regardless of where the
    local host dir physically lives (including outside the local ``$HOME``).
    """
    return Path("~") / f".{read_root_name()}"
