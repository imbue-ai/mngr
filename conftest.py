"""Root conftest for the monorepo.

Common pytest hooks (test locking, timing limits, output file redirection) are
provided by the shared module imbue.imbue_common.conftest_hooks. Each project's
conftest.py calls register_conftest_hooks(globals()) to inject them. The shared
module ensures hooks are only registered once even when multiple conftest.py files
are discovered (e.g., when running from the monorepo root).

Resource guards are discovered via the imbue_resource_guards entry point group
(see libs/resource_guards/README.md and each library's pyproject.toml). No
manual guard registration is needed here -- register_conftest_hooks() walks
the entry points so every project, root or subdir, sees the same canonical set.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())
