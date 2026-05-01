"""Resource guard registrations owned by mngr.

Discovered via the imbue_resource_guards entry point group declared in
mngr's pyproject.toml. Calling register_mngr_guards() registers every
guard whose underlying tool ships through mngr (PATH-shadowed CLIs and
the Docker SDK monkeypatch).
"""

from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.resource_guards.resource_guards import register_resource_guard


def register_mngr_guards() -> None:
    """Register every resource guard owned by mngr."""
    register_resource_guard("tmux")
    register_resource_guard("rsync")
    register_resource_guard("unison")
    register_docker_cli_guard()
    register_docker_sdk_guard()
