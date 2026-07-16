"""Non-fixture test utilities for the mngr_vultr package.

Holds the release-test opt-in flag so both ``conftest.py`` (the session-end
leak detector) and ``test_release_vultr.py`` (the test gate) read the same
value, plus the placeholder OS image id the cleanup paths use to construct a
``VultrVpsClient``. Mirrors the ``*_RELEASE_TESTS_OPT_IN`` pattern in mngr_aws,
mngr_gcp, and mngr_azure.
"""

import os
from typing import Final

# Opt-in for the Vultr release tests. Set ``MNGR_VULTR_RELEASE_TESTS=1`` to run
# them. Gating release tests behind an explicit opt-in -- rather than the mere
# presence of ``VULTR_API_KEY`` -- lets the session-end leak detector in
# ``conftest.py`` distinguish "a release run that forgot its API key" (a
# misconfiguration worth failing on) from "an ordinary unit-only run" (which
# never sets the key and must not fail). Read once at import time so both
# modules observe the same value.
VULTR_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_VULTR_RELEASE_TESTS") == "1"

# Vultr OS image id required by the ``VultrVpsClient`` constructor. Only
# ``create_instance`` reads it; the list/destroy paths that the session-end leak
# detector (``conftest.py``) and the out-of-band reaper
# (``scripts/cleanup_old_vultr_test_instances.py``) use never touch it, but the
# field is required, so those paths pass this placeholder. 2136 = Debian 12 x64.
VULTR_TEST_OS_ID: Final[int] = 2136
