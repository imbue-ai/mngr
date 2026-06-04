"""Project-level conftest for mngr_modal.

Provides test infrastructure via register_plugin_test_fixtures (the shared
HOME-isolation + temp-dir fixtures every mngr plugin uses). Modal-specific
fixtures (a setup_test_mngr_env that also loads Modal credentials,
modal_subprocess_env, session cleanup, etc.) live in imbue.mngr_modal.conftest
so consuming packages can import them via pytest_plugins.

Resource guards are discovered automatically from the resource_guards
entry point group, so no manual registration is needed here.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, the same single entry point every
# mngr plugin uses. mngr_modal's package-level conftest then layers its
# Modal-specific fixtures on top (including a setup_test_mngr_env override).
register_plugin_test_fixtures(globals())
