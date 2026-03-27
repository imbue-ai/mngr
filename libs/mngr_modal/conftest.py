"""Project-level conftest for mngr_modal.

Provides test infrastructure by inheriting from mngr's conftest. Modal-specific
fixtures (setup_test_mngr_env, modal_subprocess_env, session cleanup, etc.) live
in imbue.mngr_modal.conftest so consuming packages can import them via
pytest_plugins.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr_modal.register_guards import register_modal_guard
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_marker("tmux: marks tests that create real tmux sessions or mngr agents")
register_marker("rsync: marks tests that invoke rsync for file transfer")
register_marker("unison: marks tests that start a real unison file-sync process")
register_marker("modal: marks tests that connect to the Modal cloud service")
register_resource_guard("tmux")
register_resource_guard("rsync")
register_resource_guard("unison")
register_resource_guard("modal")
register_modal_guard()

register_conftest_hooks(globals())

# Inherit all fixtures from mngr's conftest (same pattern as mngr_claude)
pytest_plugins = ["imbue.mngr.conftest"]
