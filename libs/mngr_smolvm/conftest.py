"""Project-level conftest for mngr_smolvm.

Provides test infrastructure by inheriting from mngr's conftest.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()

# Marks tests that need a KVM-capable environment plus a smolvm build with
# btrfs data-disk support (not yet publicly distributed). They skip cleanly
# when either is missing and run for real on developer machines.
register_marker("smolvm: marks tests that require a smolvm build with KVM and data-disk support")

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())
