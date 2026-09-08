from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())
