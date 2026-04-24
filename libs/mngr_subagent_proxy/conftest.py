"""Project-level conftest for mngr_subagent_proxy.

Registers the shared pytest hooks (for --slow-tests-to-file, --coverage-to-file,
etc.) and inherits fixtures from mngr's conftest, matching the pattern used by
mngr_claude and mngr_modal.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()
register_conftest_hooks(globals())

pytest_plugins = ["imbue.mngr.conftest"]
