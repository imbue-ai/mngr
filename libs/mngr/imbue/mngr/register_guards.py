"""Resource guard registrations owned by mngr.

How resource guards are wired up across this monorepo
-----------------------------------------------------

The `resource-guards` library is generic: it doesn't know which tools the
monorepo cares about. Each library that owns an external tool declares its
guards through the `resource_guards` entry point group in its own
`pyproject.toml`, and the shared `register_conftest_hooks()` in
`imbue.imbue_common.conftest_hooks` walks that group at conftest import time
to register every guard once. Project conftests therefore never call
`register_resource_guard(...)` directly -- the set of guarded resources is a
global property of the monorepo, so a project can never silently lose
enforcement of a mark just because its conftest forgot to list it.

Currently registered:

- mngr (this module): tmux, rsync, unison, docker CLI, docker SDK
- modal_proxy (`imbue.modal_proxy.register_guards`): modal CLI + SDK
- mngr_lima (`imbue.mngr_lima.register_guards`): lima CLI

To add a new guard from a new library:

1. Implement a `register_<name>_guards()` callable in `imbue/<lib>/register_guards.py`
   that calls `register_resource_guard(...)` for binary guards and/or
   `create_sdk_method_guard(...)` / `register_sdk_guard(...)` for SDK guards.
2. Add an entry point to that library's `pyproject.toml`:

       [project.entry-points.resource_guards]
       <lib> = "imbue.<lib>.register_guards:register_<name>_guards"

3. Run `uv sync --all-packages` so the editable install picks up the entry point.

See `libs/resource_guards/README.md` for the underlying library API.
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
