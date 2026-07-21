"""End-to-end checks of the per-account plan/quota enforcement against a real ci env.

Exercises the connector's entitlements paths against the real Neon DB (lazy
row creation, plan resolution, the quota rejections) using a fresh verified
user, who -- being created after the feature-ship cutoff with a non-paid-listed
email -- must land on the explorer plan.

The lease *cap* itself (403 at max_remote_workspaces) is unit-tested only: the
ci env's pool has no baked hosts and the test fixtures have no paid-admin key
to lower a quota to zero, so the cap cannot be reached here. The lease test
below still proves the quota check-then-lease path works against the real DB
(including the per-user advisory lock).
"""

from collections.abc import Callable

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


@pytest.mark.timeout(180)
def test_fresh_account_lands_on_explorer_and_cannot_mint_llm_keys(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """A new non-paid account gets the explorer plan; its $0 LLM budget refuses key minting."""
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        account = client.get(f"{connector_url}/account", headers=_auth_header(verified_user))
        assert account.status_code == 200, f"GET /account failed: {account.text[:400]!r}"
        body = account.json()
        assert body["plan_name"] == "explorer", f"fresh account landed on {body['plan_name']!r}, not explorer"
        assert body["entitlements"]["monthly_llm_spend_usd"] == 0
        assert "ally" in body["available_plans"], "plans table is not seeded with the launch plans"

        key_response = client.post(
            f"{connector_url}/keys/create",
            headers=_auth_header(verified_user),
            json={},
        )
        assert key_response.status_code == 403, (
            f"explorer key minting should be refused, got {key_response.status_code}: {key_response.text[:400]!r}"
        )
        detail = key_response.json()["detail"]
        assert detail["code"] == "quota_exceeded"
        assert detail["entitlement"] == "monthly_llm_spend_usd"


@pytest.mark.timeout(180)
def test_ally_plan_requires_partner_access(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """Switching to ally errors with the reason for a non-paid-listed email."""
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{connector_url}/account/plan",
            headers=_auth_header(verified_user),
            json={"plan": "ally"},
        )
    assert response.status_code == 403, f"expected an eligibility refusal, got {response.status_code}"
    assert "partner access" in response.text


@pytest.mark.timeout(180)
def test_lease_quota_check_passes_through_for_under_quota_account(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """An under-quota lease reaches the pool query (exercising the real-DB quota path).

    The ci env's pool is empty, so the expected outcome is the pool's own 503
    (no capacity) -- NOT a 403 quota rejection. If a pool host ever exists,
    the lease succeeds and is released again.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{connector_url}/hosts/lease",
            headers=_auth_header(verified_user),
            json={
                "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderTestKeyForQuotaCheck",
                "host_name": "quota-check-probe",
                "attributes": {"cpus": 999999},
            },
        )
        if response.status_code == 200:
            host_db_id = response.json()["host_db_id"]
            release = client.post(f"{connector_url}/hosts/{host_db_id}/release", headers=_auth_header(verified_user))
            assert release.status_code == 200
        else:
            assert response.status_code == 503, (
                f"under-quota lease must reach the pool (503 when empty), got {response.status_code}: "
                f"{response.text[:400]!r}"
            )
            assert "quota_exceeded" not in response.text
