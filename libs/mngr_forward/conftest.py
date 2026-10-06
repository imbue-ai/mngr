import resource
from collections.abc import Iterator
from typing import Final

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_conftest_hooks(globals())

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json.
register_plugin_test_fixtures(globals())

# Comfortably above select()'s FD_SETSIZE (1024) on both macOS and Linux.
_HIGH_FD_FLOOR: Final[int] = 1500
_REQUIRED_OPEN_FILE_SOFT_LIMIT: Final[int] = 2048


@pytest.fixture
def high_fd_floor() -> Iterator[int]:
    """Lowest fd number a test may dup descriptors to, with the soft open-file limit raised to allow it."""
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit < _REQUIRED_OPEN_FILE_SOFT_LIMIT:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_REQUIRED_OPEN_FILE_SOFT_LIMIT, hard_limit))
        yield _HIGH_FD_FLOOR
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))
    else:
        yield _HIGH_FD_FLOOR
