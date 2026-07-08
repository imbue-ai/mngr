"""Project-level conftest for mngr_claude_subagent_proxy.

Registers the shared pytest hooks (for --slow-tests-to-file, --coverage-to-file,
etc.) and inherits fixtures from mngr's conftest, matching the pattern used by
mngr_claude and mngr_modal.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()
register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())
