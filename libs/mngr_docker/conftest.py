"""Project-level conftest for mngr_docker.

Provides test infrastructure by inheriting from mngr's conftest.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()

register_marker(
    "allow_warnings(match=None): opt out of the autouse 'no unexpected loguru warnings' check; "
    "if match is given, only warnings matching the regex are allowed"
)

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())
