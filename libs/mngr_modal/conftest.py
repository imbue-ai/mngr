"""Project-level conftest for mngr_modal.

Provides test infrastructure by inheriting from mngr's conftest. Modal-specific
fixtures (setup_test_mngr_env, modal_subprocess_env, session cleanup, etc.) live
in imbue.mngr_modal.conftest so consuming packages can import them via
pytest_plugins.

Resource guards are discovered automatically from the imbue_resource_guards
entry point group, so no manual registration is needed here.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())

# Inherit all fixtures from mngr's conftest (same pattern as mngr_claude)
pytest_plugins = ["imbue.mngr.conftest"]
