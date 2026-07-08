"""Project-level conftest for mngr-claude.

When running tests from libs/mngr_claude/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()
register_conftest_hooks(globals())

# Isolate HOME and pull in the shared temp-dir fixtures the same way every other
# mngr plugin does. This is what protects the real ~/.mngr / ~/.claude.json.
register_plugin_test_fixtures(globals())

# mngr_claude's tests also exercise mngr_modal's test fixtures (modal_subprocess_env,
# temp_source_dir, real_modal_provider, ...), shared via pytest_plugins. mngr_modal's
# autouse _load_modal_test_credentials fixture layers Modal tokens on top of the
# base HOME isolation above (the two set independent env vars and no longer
# collide), so real-Modal acceptance/release tests can authenticate.
pytest_plugins = ["imbue.mngr_modal.conftest"]
