"""``minds_services`` test: cheap smoke that the shared env's customer-facing routes work.

A fast, narrow test that distinguishes "env is sick" from "a specific
feature broke" when one of the heavier tests fails. Uses the
``verified_user`` fixture (admin-bypass verification) so we are not
re-running the realistic signup flow for what is supposed to be a
cheap signal.

Currently skipped -- iterates once the connector's ``/version``
endpoint and the ``verified_user`` fixture's SuperTokens admin
provisioning are both in place.
"""

from collections.abc import Callable

import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle

pytestmark = pytest.mark.minds_services


@pytest.mark.skip(
    reason=(
        "Pending: connector /version endpoint, verified_user fixture provisioning via SuperTokens "
        "admin API. See specs/minds-deployment-tests.md > Initial test inventory."
    )
)
def test_logged_in_smoke(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """Hit every connector route the desktop client uses on the home screen.

    Planned assertions, against the ``default`` shared env's URLs and
    using ``verified_user.session_token`` for auth:

    - ``GET <connector_url>/health/liveness`` returns ``{"status": "ok"}`` (200).
    - ``GET <connector_url>/version`` returns ``{"deploy_id": <non-empty>,
      "generation_id": <maybe-empty-for-dev-tier>}`` (200).
    - ``GET <connector_url>/tunnels`` returns ``[]`` (the verified_user
      has no tunnels yet) (200).
    - ``GET <connector_url>/generation`` returns ``{"generation_id": ...}`` (200).
    - ``GET <litellm_proxy_url>/health`` returns 200.

    All five together take well under a second when the env is healthy;
    when this test fails the heavier tests' failures are noise.
    """
    raise AssertionError("not implemented yet -- see skip reason")
