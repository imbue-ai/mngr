from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mng.utils.logging import suppress_warnings
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_marker("tmux: marks tests that start real tmux sessions")
register_resource_guard("tmux")

register_conftest_hooks(globals())
