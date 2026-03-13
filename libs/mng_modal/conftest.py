"""Project-level conftest for mng_modal.

Registers the Modal resource guard for tests in this package.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mng.utils.logging import suppress_warnings
from imbue.mng_modal.register_guards import register_modal_guard
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_resource_guard("modal")
register_modal_guard()

register_conftest_hooks(globals())
