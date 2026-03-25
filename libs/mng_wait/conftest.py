from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mng.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())
