"""Project-level conftest for minds.

When running tests from apps/minds/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).

Also registers the shared plugin test fixtures so tests get the standard autouse
isolation (HOME, MNGR_HOST_DIR, MNGR_PREFIX, MNGR_ROOT_NAME pointed at a per-test
temp directory, tmux server isolation, etc). Without this, tests under apps/minds/
that subprocess mngr would inherit the developer's real `~/.mngr` state.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()
register_conftest_hooks(globals())
register_plugin_test_fixtures(globals())
