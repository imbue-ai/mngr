"""Project-level conftest for mngr-pair.

When running tests from libs/mngr_pair/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

# Register guarded resources used by mngr-pair tests.
# The corresponding pytest mark is auto-registered by conftest_hooks.
register_resource_guard("unison")

register_conftest_hooks(globals())
