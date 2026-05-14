"""Filesystem paths for the dynamic dev env subsystem.

Per-dev-env config + secrets live in ``~/.<root>/envs/<name>.toml`` so they
sit next to (and inherit the ``MINDS_ROOT_NAME``-isolation of) the rest of
the minds data directory. The file is ``chmod 600`` so it never spills.
"""

from pathlib import Path

from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.envs.primitives import DevEnvName


def dev_envs_dir(*, root_name: str | None = None) -> Path:
    """Return ``~/.<root>/envs/``."""
    if root_name is None:
        root_name = resolve_minds_root_name()
    return minds_data_dir_for(root_name) / "envs"


def dev_env_file(name: DevEnvName, *, root_name: str | None = None) -> Path:
    """Return ``~/.<root>/envs/<name>.toml``."""
    return dev_envs_dir(root_name=root_name) / f"{name}.toml"
