"""Project-level conftest for mngr_donate."""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()
register_conftest_hooks(globals())
register_plugin_test_fixtures(globals())
