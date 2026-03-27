"""Project-level conftest for mngr-claude.

When running tests from libs/mngr_claude/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_marker("modal: marks tests that connect to the Modal cloud service")

register_conftest_hooks(globals())

# Inherit fixtures from mngr's conftest (base test infrastructure) and
# mngr_modal's conftest (modal token loading, modal_subprocess_env, etc.)
pytest_plugins = ["imbue.mngr.conftest", "imbue.mngr_modal.conftest"]
