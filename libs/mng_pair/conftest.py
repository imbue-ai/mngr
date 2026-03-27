"""Project-level conftest for mng-pair.

When running tests from libs/mng_pair/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mng.utils.logging import suppress_warnings
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

# Register markers and guarded resources used by mng-pair tests.
register_marker("unison: marks tests that start a real unison file-sync process")
register_resource_guard("unison")

register_conftest_hooks(globals())
