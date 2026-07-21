"""Non-fixture test utilities for the mngr_vultr package.

Holds the release-test opt-in flag so both ``conftest.py`` (the session-end
leak detector) and ``test_release_vultr.py`` (the test gate) read the same
value, plus the placeholder OS image id the cleanup paths use to construct a
``VultrVpsClient``. Mirrors the ``*_RELEASE_TESTS_OPT_IN`` pattern in mngr_aws,
mngr_gcp, and mngr_azure.
"""

import os
from typing import Final

# Opt-in for the Vultr release tests. Set ``MNGR_VULTR_RELEASE_TESTS=1`` to run them.
VULTR_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_VULTR_RELEASE_TESTS") == "1"

# Placeholder OS image id for cleanup-path ``VultrVpsClient`` construction (2136 = Debian 12 x64).
VULTR_TEST_OS_ID: Final[int] = 2136
