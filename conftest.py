"""Root conftest for the monorepo.

Common pytest hooks (test locking, timing limits, output file redirection) are
provided by the shared module imbue.imbue_common.conftest_hooks. Each project's
conftest.py calls register_conftest_hooks(globals()) to inject them. The shared
module ensures hooks are only registered once even when multiple conftest.py files
are discovered (e.g., when running from the monorepo root).

Resource guards are discovered via the resource_guards entry point group;
no manual guard registration is needed here. See the docstring of
libs/mngr/imbue/mngr/register_guards.py for how guards are wired up in this
monorepo and how to add new ones.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())

# apps/minds_evals is a standalone uv project, not a workspace member (see the
# root pyproject's [tool.uv.workspace].exclude), so neither its package nor
# harbor exists in this venv: collecting its tests raises ModuleNotFoundError,
# which is a hard collection error that aborts the entire run (including
# offload's --collect-only discovery phase). Its suite runs under its own
# project via `just test-minds-evals`.
#
# Two things about the form. It lives here rather than in addopts because CI
# invocations that pass --override-ini='addopts=...' (see
# .github/workflows/release-tests.yml) discard addopts entirely. And it must be
# the glob variant matching *files*: pytest's ignore hook compares plain
# collect_ignore entries by exact path equality against each candidate, and
# `testpaths = ["apps/*", ...]` hands pytest the directory as an explicit
# initial argument, so a bare directory entry never matches anything.
collect_ignore_glob = ["apps/minds_evals/*"]
