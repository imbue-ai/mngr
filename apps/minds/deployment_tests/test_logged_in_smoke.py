"""``minds_services`` test: cheap smoke that the shared env's customer-facing routes work.

A fast, narrow test that distinguishes "env is sick" from "a specific
feature broke" when one of the heavier tests fails. Uses the
``verified_user`` fixture (admin-bypass verification) so we are not
re-running the realistic signup flow for what is supposed to be a
cheap signal.
"""

from collections.abc import Callable

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = pytest.mark.minds_services


_REQUEST_TIMEOUT_SECONDS = 30.0
# Generous per-test timeout so the wait_for_env_ready cold-boot poll (up
# to 60s for the connector + 60s for the litellm proxy) plus the actual
# HTTP assertions all fit. Overrides the pyproject-wide 10s default,
# which is sized for in-process unit tests, not live-env HTTP calls.
_TEST_TIMEOUT_SECONDS = 180


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_logged_in_smoke(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """Hit every connector route the desktop client uses on the home screen.

    Five assertions against the ``default`` shared env's URLs, using
    ``verified_user.session_token`` as the Bearer token for the auth'd
    routes:

    1. ``GET <connector_url>/health/liveness`` returns ``{"status": "ok"}`` (200).
    2. ``GET <connector_url>/version`` returns ``{"deploy_id": <non-empty>, "generation_id": <maybe-empty-for-dev>}`` (200).
    3. ``GET <connector_url>/generation`` returns ``{"generation_id": ...}`` (200).
    4. ``GET <litellm_proxy_url>/health/liveness`` returns 200.
    5. ``GET <connector_url>/tunnels`` with the verified-user session token returns ``[]`` (200; the user has no tunnels yet).

    All five together take well under a second when the env is healthy;
    when this test fails the heavier tests' failures are noise.
    """
    env = shared_env("default")
    # Defensive: wait until the env is reachable before any assertions, so
    # cold-boot / stale-container windows don't surface as test flakes.
    # The session-autouse fixture has already swept stale test-*@example.test
    # users from this env's SuperTokens app by the time we reach this line.
    wait_for_env_ready(env)

    connector_url = str(env.urls.connector_url).rstrip("/")
    litellm_proxy_url = str(env.urls.litellm_proxy_url).rstrip("/")

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        liveness = client.get(f"{connector_url}/health/liveness")
        assert liveness.status_code == 200, liveness.text
        assert liveness.json() == {"status": "ok"}, liveness.text

        version = client.get(f"{connector_url}/version")
        assert version.status_code == 200, version.text
        version_body = version.json()
        assert "deploy_id" in version_body and version_body["deploy_id"], version_body
        assert "generation_id" in version_body, version_body

        generation = client.get(f"{connector_url}/generation")
        assert generation.status_code == 200, generation.text
        assert "generation_id" in generation.json(), generation.text

        litellm_liveness = client.get(f"{litellm_proxy_url}/health/liveness")
        assert litellm_liveness.status_code == 200, litellm_liveness.text

        tunnels = client.get(
            f"{connector_url}/tunnels",
            headers={"Authorization": f"Bearer {verified_user.session_token.get_secret_value()}"},
        )
        assert tunnels.status_code == 200, tunnels.text
        assert tunnels.json() == [], tunnels.text
