from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_marker("tmux: marks tests that invoke tmux via agent discovery")
register_plugin_test_fixtures(globals())
