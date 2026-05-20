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

from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr_aws.client import AWS_TEST_INSTANCE_LABEL_PREFIX
from imbue.mngr_aws.client import AwsVpsClient

# ``mngr_vps_docker.instance`` builds every EC2 ``Name`` tag as
# ``f"mngr-{agent_name}"``, so to make a Name tag that starts with the
# production guard's expected prefix, the agent name must start with that
# prefix minus the ``"mngr-"`` literal.
_MNGR_LABEL_PREFIX: Final[str] = "mngr-"
assert AWS_TEST_INSTANCE_LABEL_PREFIX.startswith(_MNGR_LABEL_PREFIX), (
    f"AWS_TEST_INSTANCE_LABEL_PREFIX must start with {_MNGR_LABEL_PREFIX!r} "
    f"(mngr_vps_docker prepends it to every label); got "
    f"{AWS_TEST_INSTANCE_LABEL_PREFIX!r}."
)

# ``Name`` tag prefix used by release tests when naming their hosts; the
# session-end orphan scan uses this prefix to find instances that escaped
# any per-test cleanup. Derived from the production label prefix so the
# guard in ``client.create_instance`` and the conftest leak scanner cannot
# drift out of alignment with the names tests actually generate.
AWS_TEST_NAME_PREFIX: Final[str] = AWS_TEST_INSTANCE_LABEL_PREFIX[len(_MNGR_LABEL_PREFIX) :]

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

    _cached_ec2_client_override: Any = PrivateAttr(default=None)

    def _ec2(self) -> Any:
        return self.stubbed_ec2_client
