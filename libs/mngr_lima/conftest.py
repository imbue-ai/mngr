"""Project-level conftest for mngr_lima.

Provides test infrastructure by inheriting from mngr's conftest.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_marker("lima: marks tests that create real Lima VMs")
register_resource_guard("lima")

register_conftest_hooks(globals())

# Inherit all fixtures from mngr's conftest
pytest_plugins = ["imbue.mngr.conftest"]
