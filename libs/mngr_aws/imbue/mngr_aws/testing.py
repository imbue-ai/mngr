"""Shared test helpers and constants for mngr_aws.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_aws``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors the role of ``libs/mngr_modal/imbue/mngr_modal/constants.py`` plus its
test-helper analogue, folded into one module since both are very small.
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
#   1. ``test_release_aws.py`` writes it into a tmp-path settings.toml
#      (``[providers.aws] auto_shutdown_minutes``) so cloud-init runs
#      ``shutdown -P +N`` on every test instance.
#   2. ``conftest.py`` derives the orphan-scan grace period from this value
#      so the session-end leak detector never race-kills an in-flight test
#      on a parallel worker.
# If these ever drift, the cloud-init backstop can fire after the leak
# detector has already failed the session, or the leak detector can kill
# instances that the auto-shutdown timer would have cleaned up on its own.
AWS_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES: Final[int] = 60


def aws_credentials_available() -> bool:
    """Return True if AWS credentials are plausibly present in the environment.

    Used to gate release tests (skipif) and the session-end cleanup hook
    (no-op when credentials are absent). Only checks the two env-var
    families that are sufficient for boto3's default chain to find
    credentials without further configuration -- this is intentionally a
    fast, non-network check, not a full boto3 ``get_credentials`` probe.
    """
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")) or bool(os.environ.get("AWS_PROFILE"))
