"""End-to-end checks of the per-account plan/quota enforcement against a real ci env.

Exercises the connector's entitlements paths against the real Neon DB (lazy
row creation, plan resolution, the quota rejections) using a fresh verified
user, who -- being created after the feature-ship cutoff with a non-paid-listed
email -- must land on the explorer plan.

The lease *cap* itself (403 at max_remote_workspaces) is unit-tested only: the
ci env's pool has no baked hosts and the test fixtures have no admin key
to lower a quota to zero, so the cap cannot be reached here. The lease test
below still proves the quota check-then-lease path works against the real DB
(including the per-user advisory lock).
"""

from collections.abc import Callable

import httpx
import psycopg2
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0

# A comfortably-large max_total_bucket_bytes the grant-cycle test restores
# the shared user's entitlement to (both mid-test and on any failure path).
_SANE_STORAGE_LIMIT_BYTES = 50 * 1024**3


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _set_storage_limit_bytes(env: SharedEnvHandle, user_id: str, value: int) -> None:
    """Write the user's max_total_bucket_bytes entitlement directly in the env's DB.

    The deployment-test fixtures carry no admin API key, so over-quota
    states are induced through the pool DSN instead of the admin API. A
    limit of ``-1`` makes even an empty account measurably over quota.
    """
    conn = psycopg2.connect(env.neon_host_pool_dsn.get_secret_value())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE account_entitlements SET max_total_bucket_bytes = %s WHERE user_id = %s",
                    (value, user_id),
                )
                assert cur.rowcount == 1, f"no entitlements row for user {user_id!r} (GET /account not called yet?)"
    finally:
        conn.close()


def _delete_cleanup_grants(env: SharedEnvHandle, user_id: str) -> None:
    """Drop the test user's grant rows so repeated runs never touch the failed-grant budget."""
    conn = psycopg2.connect(env.neon_host_pool_dsn.get_secret_value())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM r2_cleanup_grants WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


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


@pytest.mark.timeout(300)
def test_storage_cleanup_grant_cycle(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    """Full storage-enforcement cycle against the real connector + DB + Cloudflare.

    Downgrade (recheck while over quota flips the bucket key read-only) ->
    cleanup grant restores it -> recheck settles the grant and, with the
    quota back to normal, leaves the key writable. The recheck endpoint
    applies the same enforcement the hourly sweep's confirm-and-flip path
    uses, so this also exercises migration 015, the per-user enforcement
    lock, and the live REST usage measurement end to end.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)
    user_id = str(verified_user.supertokens_user_id)

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        # Materialize the entitlements row, then create a bucket whose single
        # key is the thing enforcement flips.
        account = client.get(f"{connector_url}/account", headers=_auth_header(verified_user))
        assert account.status_code == 200, f"GET /account failed: {account.text[:400]!r}"
        created = client.post(
            f"{connector_url}/buckets",
            headers=_auth_header(verified_user),
            json={"name": "grant-cycle-probe"},
        )
        assert created.status_code == 200, f"bucket create failed: {created.text[:400]!r}"
        try:
            # Force the account over quota (empty usage > limit of -1) and
            # apply enforcement: the key must come back downgraded.
            _set_storage_limit_bytes(env, user_id, -1)
            downgraded = client.post(f"{connector_url}/account/storage-recheck", headers=_auth_header(verified_user))
            assert downgraded.status_code == 200, f"recheck failed: {downgraded.text[:400]!r}"
            downgraded_body = downgraded.json()
            assert downgraded_body["is_over_quota"] is True
            assert downgraded_body["keys"], "the created bucket's key should be listed"
            assert all(key["enforced_access"] == "read" for key in downgraded_body["keys"])

            # A cleanup grant restores the key in place.
            grant = client.post(f"{connector_url}/account/storage-cleanup-grant", headers=_auth_header(verified_user))
            assert grant.status_code == 200, f"cleanup grant failed: {grant.text[:400]!r}"
            grant_body = grant.json()
            assert grant_body["status"] == "granted"
            assert all(key["enforced_access"] is None for key in grant_body["keys"])

            # Back under quota, the recheck settles the grant and leaves the
            # key writable.
            _set_storage_limit_bytes(env, user_id, _SANE_STORAGE_LIMIT_BYTES)
            settled = client.post(f"{connector_url}/account/storage-recheck", headers=_auth_header(verified_user))
            assert settled.status_code == 200, f"settling recheck failed: {settled.text[:400]!r}"
            settled_body = settled.json()
            assert settled_body["is_over_quota"] is False
            assert settled_body["is_grant_settled"] is True
            assert all(key["enforced_access"] is None for key in settled_body["keys"])
        finally:
            # A mid-test failure must not leave the shared user's entitlement
            # at -1 (every later bucket create would 403 on the storage gate).
            _set_storage_limit_bytes(env, user_id, _SANE_STORAGE_LIMIT_BYTES)
            cleanup = client.delete(f"{connector_url}/buckets/grant-cycle-probe", headers=_auth_header(verified_user))
            assert cleanup.status_code == 200, f"bucket cleanup failed: {cleanup.text[:400]!r}"
            _delete_cleanup_grants(env, user_id)


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
