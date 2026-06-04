"""Project-level conftest for mngr_sbx.

Provides test infrastructure by inheriting from mngr's conftest.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())

pytest_plugins = ["imbue.mngr.conftest"]
