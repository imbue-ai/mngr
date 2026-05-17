"""Module-level constants for mngr_aws.

Kept separate from ``conftest.py`` so multiple test modules
(``conftest.py`` and ``test_release_aws.py``) can share a single source of
truth without importing from a ``conftest.py`` -- which is a pytest
anti-pattern, since conftest files are auto-discovered, not designed for
direct import. Mirrors the ``libs/mngr_modal/imbue/mngr_modal/constants.py``
pattern.
"""

import os
from typing import Final

# ``Name`` tag prefix used by release tests when naming their hosts; the
# session-end orphan scan uses this prefix to find instances that escaped
# any per-test cleanup.
AWS_TEST_NAME_PREFIX: Final[str] = "test-aws-"

# Region used by the AWS release tests and the session-end leak scan. Tests
# can override via ``AWS_REGION``; defaults to ``us-east-1`` to match the
# rest of the suite. Read once at import time so conftest and
# test_release_aws observe the same value.
AWS_DEFAULT_REGION: Final[str] = os.environ.get("AWS_REGION", "us-east-1")

# Release-test opt-in flag. Mirrors the gate that ``test_release_aws.py``
# uses on ``pytestmark`` and that ``conftest.py`` uses to suppress the
# session-end orphan scan when no release tests were requested. Read once at
# import time so both modules observe the same value.
AWS_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_AWS_RELEASE_TESTS") == "1"

# Single source of truth for the release-test instance lifetime. Used in two
# places that must stay aligned:
#   1. ``test_release_aws.py`` propagates this into ``MNGR_AWS_AUTO_SHUTDOWN_MINUTES``
#      so cloud-init runs ``shutdown -P +N`` on every test instance.
#   2. ``conftest.py`` derives the orphan-scan grace period from this value
#      so the session-end leak detector never race-kills an in-flight test
#      on a parallel worker.
# If these ever drift, the cloud-init backstop can fire after the leak
# detector has already failed the session, or the leak detector can kill
# instances that the auto-shutdown timer would have cleaned up on its own.
AWS_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES: Final[int] = 60
