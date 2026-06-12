"""Project-level conftest for mngr_lima.

Provides test infrastructure by inheriting from mngr's conftest.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

suppress_warnings()

# Marks tests that need `limactl` + `qemu` available (and root, for the
# release test that self-installs them). Skipped by default in CI's
# unit/integration sets; the release test orchestrator picks them up.
register_marker("lima: marks tests that require limactl + qemu (and root to install them in release CI)")

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())
