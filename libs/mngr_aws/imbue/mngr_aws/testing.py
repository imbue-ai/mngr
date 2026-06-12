"""Shared test helpers and constants for mngr_aws.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_aws``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors the role of ``libs/mngr_modal/imbue/mngr_modal/constants.py`` plus its
test-helper analogue, folded into one module since both are very small.
"""

import os
from typing import Any
from typing import Final

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from pydantic import Field

from imbue.mngr_aws.client import AwsVpsClient

# Optional prefix release tests use for their agent names so leaked instances
# (should the scanner ever fail) are still visually identifiable as test-owned.
# Cleanup logic does NOT depend on this -- ``AwsVpsClient.create_instance``
# tags pytest-launched instances with ``mngr-pytest-launched=true`` and the
# conftest scanner filters on that tag.
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
#      (``[providers.aws] auto_shutdown_seconds``) so cloud-init runs
#      ``shutdown -P +N`` on every test instance.
#   2. ``conftest.py`` derives the orphan-scan grace period from this value
#      so the session-end leak detector never race-kills an in-flight test
#      on a parallel worker.
# If these ever drift, the cloud-init backstop can fire after the leak
# detector has already failed the session, or the leak detector can kill
# instances that the auto-shutdown timer would have cleaned up on its own.
AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS: Final[int] = 60 * 60


def clear_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``AWS_*`` env var so credential-chain probes start clean.

    boto3's chain inspects a dozen-plus env vars; clearing only those each
    test cares about by name leaks knowledge of the chain into tests. Wipe
    them all and let the test re-set only what it wants. Used by both the
    config-level credential resolution tests and the backend-level
    discovery-warning tests.
    """
    for key in list(os.environ.keys()):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)


def aws_credentials_available() -> bool:
    """Return True iff boto3's default credential chain can resolve credentials.

    Used to gate release tests (skipif) and the session-end cleanup hook
    (no-op when credentials are absent). Walks the full boto3 chain (env
    vars, shared credentials file, AWS_PROFILE, EC2 IMDS), matching what
    ``AwsProviderConfig.get_session`` does at provider-construction time
    -- so the gate and the production code agree on what counts as
    "available".

    ``session.get_credentials()`` does not make a network call when env or
    file sources resolve; it only contacts IMDS as a last resort.
    """
    try:
        return boto3.Session().get_credentials() is not None
    except BotoCoreError:
        return False


class _StubbedAwsVpsClient(AwsVpsClient):
    """Test-only AwsVpsClient that bypasses session-based EC2 client construction.

    Unit tests use ``botocore.stub.Stubber`` to intercept boto3 calls, but
    the Stubber must wrap the same client instance that the code under test
    uses. Production ``AwsVpsClient`` builds the EC2 client lazily from its
    boto3 Session; this subclass exposes a constructor field that callers
    can populate with a pre-built (and stubber-wrapped) client. Keeping the
    test-only injection out of the production model means production code
    never carries a field whose sole purpose is test orchestration.
    """

    stubbed_ec2_client: Any = Field(
        description="Pre-built EC2 client to use instead of session.client('ec2'). "
        "Typically a Stubber-wrapped client created by the test fixture."
    )

    def _ec2(self) -> Any:
        return self.stubbed_ec2_client
