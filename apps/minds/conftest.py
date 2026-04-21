"""Project-level conftest for minds.

When running tests from apps/minds/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).

Also registers the shared plugin test fixtures so tests get the standard autouse
isolation (HOME, MNGR_HOST_DIR, MNGR_PREFIX, MNGR_ROOT_NAME pointed at per-test temp
values, tmux server isolation). The MNGR_PREFIX the shared fixture picks by default
is `mngr_<hex>-`, which the Modal backend guard rejects (it only accepts underscore-
prefixed env names beginning with `mngr_test-`); minds tests spawn real mngr
subprocesses that may create Modal envs, so the `mngr_test_prefix` fixture is
overridden here to produce the `mngr_test-YYYY-MM-DD-HH-MM-SS-` format that the
backend guard AND the CI cleanup script (cleanup_old_modal_test_environments.py)
both recognize.
"""

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import generate_test_environment_name

suppress_warnings()
register_conftest_hooks(globals())
register_plugin_test_fixtures(globals())


@pytest.fixture
def mngr_test_prefix() -> str:
    """Override the shared mngr_test_prefix to use `mngr_test-YYYY-MM-DD-HH-MM-SS-`.

    The shared fixture defaults to `mngr_<hex>-` which the Modal backend guard
    rejects. Minds tests spawn real mngr subprocesses that can create Modal envs,
    so the prefix needs to match the format the guard AND the cleanup script
    recognize.
    """
    return f"{generate_test_environment_name()}-"
