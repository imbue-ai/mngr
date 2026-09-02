"""Tests for the connector HTTP client.

We mount an httpx MockTransport on the underlying transport so the calls
never go to the network; this isolates the tests from connector availability
and makes them deterministic.
"""

import ast
import inspect
import json as _json

import httpx
import pytest
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.connector import client as connector_client_module
from imbue.mngr_imbue_cloud.connector.client import CLIENT_ID_HEADER
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.client import create_litellm_key_rotating_on_exists
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.errors import ImbueCloudAccountError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAccountSuspendedError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketExistsError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketLimitError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotFoundError
from imbue.mngr_imbue_cloud.errors import ImbueCloudCleanupGrantBudgetError
from imbue.mngr_imbue_cloud.errors import ImbueCloudClientTooOldError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudEmailNotVerifiedError
from imbue.mngr_imbue_cloud.errors import ImbueCloudKeyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudQuotaExceededError
from imbue.mngr_imbue_cloud.errors import ImbueCloudRecordFormatTooNewError
from imbue.mngr_imbue_cloud.errors import ImbueCloudShareError
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError
from imbue.mngr_imbue_cloud.errors import ImbueCloudUnreachableError
from imbue.mngr_imbue_cloud.errors import WorkspacesEndpointUnavailableError
from imbue.mngr_imbue_cloud.wire_types import LiteLLMKeyInfo
from imbue.mngr_imbue_cloud.wire_types import LiteLLMKeyMaterial
from imbue.mngr_imbue_cloud.wire_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.wire_types import SyncWorkspaceRecord
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus


def _make_client(handler) -> tuple[ImbueCloudConnectorClient, httpx.MockTransport]:
    transport = httpx.MockTransport(handler)

    # Patch httpx module-level functions to use the transport for the duration of the test.
    # The client uses module-level httpx.* calls; intercept them via monkeypatch in tests.
    return ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com")), transport


def _install_fake_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route the client's module-level httpx.* calls through a MockTransport.

    Thin alias over ``_install_mock_httpx`` (defined below) for call sites
    that construct their own client; sharing the one patch loop keeps the
    test-patching ratchet count minimal.
    """
    _install_mock_httpx(monkeypatch, handler)


def test_lease_host_503_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # An absent region must not be sent so the connector treats the lease as
        # region-agnostic.
        body = _json.loads(request.content)
        assert "region" not in body
        return httpx.Response(503, json={"detail": "no match"})

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudLeaseUnavailableError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", "my-host")


def test_lease_host_success_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["attributes"] == {"cpus": 2}
        assert body["ssh_public_key"] == "ssh-ed25519 AAAA"
        assert body["host_name"] == "my-host"
        # The hard region rides alongside attributes as a top-level field when set.
        assert body["region"] == "US-EAST-VA"
        return httpx.Response(
            200,
            json={
                "host_db_id": "00000000-0000-0000-0000-000000000001",
                "vps_address": "10.0.0.1",
                "ssh_port": 22,
                "ssh_user": "root",
                "container_ssh_port": 2222,
                "agent_id": "agent-abc",
                "host_id": "host-xyz",
                "host_name": "my-host",
                "attributes": {"cpus": 2},
            },
        )

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    result = client.lease_host(
        SecretStr("tok"),
        LeaseAttributes(cpus=2),
        "ssh-ed25519 AAAA",
        "my-host",
        region="US-EAST-VA",
    )
    assert result.vps_address == "10.0.0.1"
    assert result.agent_id == "agent-abc"
    assert result.host_name == "my-host"
    assert result.attributes == {"cpus": 2}


def test_lease_host_retries_connect_error_then_succeeds() -> None:
    # ConnectError is a connect-phase failure (e.g. DNS EAI_NONAME); it is retried.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("simulated DNS failure", request=request)
        return httpx.Response(
            200,
            json={
                "host_db_id": "00000000-0000-0000-0000-000000000001",
                "vps_address": "10.0.0.1",
                "ssh_port": 22,
                "ssh_user": "root",
                "container_ssh_port": 2222,
                "agent_id": "agent-abc",
                "host_id": "host-xyz",
                "host_name": "my-host",
                "attributes": {"cpus": 2},
            },
        )

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    result = client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", "my-host")
    assert result.vps_address == "10.0.0.1"
    assert calls["n"] == 2


def test_lease_host_does_not_retry_post_send_error() -> None:
    # ReadError is a post-send failure, so a non-idempotent lease must not retry it.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("simulated post-send failure", request=request)

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    with pytest.raises(ImbueCloudUnreachableError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", "my-host")
    assert calls["n"] == 1


def test_auth_signin_transport_error_raises_typed_auth_error() -> None:
    # A transport failure surfaces as a typed ImbueCloudAuthError, not a raw httpx error.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("simulated post-send failure", request=request)

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    with pytest.raises(ImbueCloudAuthError):
        client.auth_signin("alice@imbue.com", "hunter2")
    assert calls["n"] == 1


def test_rename_host_success_posts_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"host_db_id": "00000000-0000-0000-0000-000000000009", "host_name": "new-name"},
        )

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    client.rename_host(SecretStr("tok"), "00000000-0000-0000-0000-000000000009", "new-name")

    assert captured["url"] == "https://example.com/hosts/00000000-0000-0000-0000-000000000009/rename"
    assert captured["body"] == {"host_name": "new-name"}


def test_rename_host_error_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudConnectorError):
        client.rename_host(SecretStr("tok"), "00000000-0000-0000-0000-000000000009", "new-name")


def test_unauthenticated_responses_raise_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no token"})

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudAuthError):
        client.list_hosts(SecretStr("tok"))


def test_500_lease_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudConnectorError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=1), "ssh-ed25519 X", "my-host")


# -- Auth email verification --


def test_auth_is_email_verified_sends_bearer_token_and_parses_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/email/is-verified"
        assert request.headers["Authorization"] == "Bearer at-secret"
        assert _json.loads(request.content) == {"email": "alice@imbue.com"}
        return httpx.Response(200, json={"verified": True})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    assert client.auth_is_email_verified(SecretStr("at-secret"), "alice@imbue.com") is True


def test_auth_send_verification_email_reports_cooldown_suppression() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/email/send-verification"
        assert request.headers["Authorization"] == "Bearer at-secret"
        assert _json.loads(request.content) == {"email": "alice@imbue.com"}
        return httpx.Response(200, json={"status": "OK", "sent": False})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    assert client.auth_send_verification_email(SecretStr("at-secret"), "alice@imbue.com") is False


def test_auth_send_verification_email_raises_on_missing_sent() -> None:
    """A 2xx body without a 'sent' bool is a broken contract, not a cooldown suppression."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK"})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    with pytest.raises(ImbueCloudAuthError, match="Malformed send-verification response"):
        client.auth_send_verification_email(SecretStr("at-secret"), "alice@imbue.com")


def test_auth_is_email_verified_raises_on_missing_verified() -> None:
    """A 2xx body without a 'verified' bool is a broken contract, not "not verified"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK"})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    with pytest.raises(ImbueCloudAuthError, match="Malformed is-verified response"):
        client.auth_is_email_verified(SecretStr("at-secret"), "alice@imbue.com")


def test_auth_is_email_verified_raises_auth_error_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid token"})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    with pytest.raises(ImbueCloudAuthError):
        client.auth_is_email_verified(SecretStr("stale"), "alice@imbue.com")


# -- R2 buckets --
#
# Same MockTransport approach as above, but patched through a single loop so the
# monkeypatch ratchet counts one occurrence regardless of how many HTTP verbs
# the bucket routes exercise.


def _install_mock_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> ImbueCloudConnectorClient:
    transport = httpx.MockTransport(handler)

    def _make(method_name: str):
        def _call(*args, **kwargs):
            with httpx.Client(transport=transport) as inner:
                return inner.request(method_name, *args, **kwargs)

        return _call

    for method_name in ("POST", "GET", "DELETE", "PUT"):
        monkeypatch.setattr(httpx, method_name.lower(), _make(method_name))
    return ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))


def _bucket_create_response() -> dict:
    endpoint = "https://acct.r2.cloudflarestorage.com"
    return {
        "bucket": {"bucket_name": "u--data", "s3_endpoint": endpoint},
        "key": {
            "access_key_id": "akid1",
            "secret_access_key": "deadbeef",
            "s3_endpoint": endpoint,
            "bucket_name": "u--data",
            "access": "readwrite",
        },
    }


def test_create_bucket_parses_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/buckets"
        assert _json.loads(request.content) == {"name": "data", "access": "readwrite"}
        return httpx.Response(200, json=_bucket_create_response())

    client = _install_mock_httpx(monkeypatch, handler)
    result = client.create_bucket(SecretStr("tok"), "data", "readwrite")
    assert result.bucket.bucket_name == "u--data"
    assert result.key.access_key_id == "akid1"
    assert result.key.secret_access_key.get_secret_value() == "deadbeef"
    assert result.key.access == "readwrite"


def test_create_bucket_exists_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Bucket already exists: u--data"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudBucketExistsError):
        client.create_bucket(SecretStr("tok"), "data", "readwrite")


def test_create_bucket_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Account is at the maximum of 50 buckets; destroy one first."})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudBucketLimitError):
        client.create_bucket(SecretStr("tok"), "data", "readwrite")


def test_destroy_bucket_not_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Bucket is not empty: u--data. Empty it before destroying."})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudBucketNotEmptyError):
        client.destroy_bucket(SecretStr("tok"), "data")


def test_get_bucket_info_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Bucket not found: u--data"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudBucketNotFoundError):
        client.get_bucket_info(SecretStr("tok"), "data")


def test_list_buckets_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/buckets"
        return httpx.Response(
            200, json=[{"bucket_name": "u--a", "s3_endpoint": "https://acct.r2.cloudflarestorage.com"}]
        )

    client = _install_mock_httpx(monkeypatch, handler)
    items = client.list_buckets(SecretStr("tok"))
    assert [b.bucket_name for b in items] == ["u--a"]


def test_roll_bucket_key_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/buckets/data/roll-key"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "access_key_id": "akid2",
                "secret_access_key": "s2",
                "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
                "bucket_name": "u--data",
                "access": "readwrite",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    material = client.roll_bucket_key(SecretStr("tok"), "data")
    assert material.access_key_id == "akid2"
    assert material.secret_access_key.get_secret_value() == "s2"


def test_list_bucket_keys_account_wide_uses_bucket_keys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[])

    client = _install_mock_httpx(monkeypatch, handler)
    client.list_bucket_keys(SecretStr("tok"), None)
    assert seen["path"] == "/bucket-keys"


def test_list_bucket_keys_per_bucket_uses_scoped_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json=[
                {
                    "access_key_id": "akid",
                    "bucket_name": "u--data",
                    "access": "readwrite",
                    "alias": "default",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )

    client = _install_mock_httpx(monkeypatch, handler)
    items = client.list_bucket_keys(SecretStr("tok"), "data")
    assert seen["path"] == "/buckets/data/keys"
    assert items[0].access_key_id == "akid"


def test_quota_exceeded_403_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connector's structured quota rejection surfaces as ImbueCloudQuotaExceededError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "quota_exceeded",
                    "entitlement": "max_buckets",
                    "limit": 5,
                    "current": 5,
                    "message": "Quota exceeded: this account allows 5 buckets and 5 are already in use.",
                }
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudQuotaExceededError) as exc_info:
        client.create_bucket(SecretStr("tok"), "one-more", "readwrite")
    assert exc_info.value.entitlement == "max_buckets"
    assert exc_info.value.limit == 5
    assert exc_info.value.current == 5


def test_get_account_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account"
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "email": "alice@imbue.com",
                "plan_name": "ally",
                "entitlements": {
                    "max_remote_workspaces": 10,
                    "max_total_workspaces": 50,
                    "max_buckets": 20,
                    "max_total_bucket_bytes": 536870912000,
                    "monthly_llm_spend_usd": 1000.0,
                    "max_active_synced_workspaces": 200,
                    # The connector serves these tunnel-era compat zeros for
                    # v0.3.11 clients; this client must tolerate them.
                    "max_tunnels": 0,
                    "max_services_per_tunnel": 0,
                },
                "usage": {
                    "remote_workspaces": 2,
                    "total_workspaces": 3,
                    "buckets": 1,
                    "total_bucket_bytes": 12345,
                    "llm_spend_usd_this_period": 42.5,
                    "llm_budget_resets_at": "2026-08-01T00:00:00Z",
                    "active_synced_workspaces": 4,
                    "tunnels": 0,
                },
                "available_plans": ["ally", "explorer"],
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    info = client.get_account(SecretStr("tok"))
    assert info.plan_name == "ally"
    assert info.entitlements.max_remote_workspaces == 10
    assert info.entitlements.max_total_workspaces == 50
    assert info.usage.llm_spend_usd_this_period == 42.5
    assert info.usage.total_workspaces == 3
    assert info.available_plans == ("ally", "explorer")


def test_create_storage_cleanup_grant_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/storage-cleanup-grant"
        return httpx.Response(
            200,
            json={
                "status": "granted",
                "expires_at": "2026-07-21T13:00:00+00:00",
                "baseline_bytes": 1000,
                "keys": [
                    {
                        "access_key_id": "akid",
                        "bucket_name": "u--data",
                        "access": "readwrite",
                        "alias": "default",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "enforced_access": None,
                    }
                ],
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    grant = client.create_storage_cleanup_grant(SecretStr("tok"))
    assert grant.status == "granted"
    assert grant.baseline_bytes == 1000
    assert grant.keys[0].enforced_access is None


def test_create_storage_cleanup_grant_budget_exhausted_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "cleanup_grant_budget_exhausted",
                    "limit": 5,
                    "current": 5,
                    "window_hours": 24,
                    "message": "Cleanup-grant budget exhausted",
                }
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudCleanupGrantBudgetError) as exc_info:
        client.create_storage_cleanup_grant(SecretStr("tok"))
    assert exc_info.value.limit == 5
    assert exc_info.value.current == 5
    assert exc_info.value.window_hours == 24


def test_recheck_storage_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/storage-recheck"
        return httpx.Response(
            200,
            json={
                "usage_bytes": 40,
                "limit_bytes": 100,
                "is_over_quota": False,
                "is_grant_settled": True,
                "keys": [],
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    result = client.recheck_storage(SecretStr("tok"))
    assert result.usage_bytes == 40
    assert result.is_over_quota is False
    assert result.is_grant_settled is True


def test_admin_run_r2_sweep_scopes_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["email"] = request.url.params.get("email", "")
        return httpx.Response(200, json={"status": "completed", "counters": {"keys_downgraded": 1}})

    client = _install_mock_httpx(monkeypatch, handler)
    body = client.admin_run_r2_sweep(SecretStr("admin-key"), "somebody@example.com")
    assert seen["path"] == "/admin/sweep/r2"
    assert seen["email"] == "somebody@example.com"
    assert body["counters"]["keys_downgraded"] == 1


def test_list_bucket_keys_parses_enforced_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep's enforcement marker round-trips into the client's key metadata."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "access_key_id": "akid",
                    "bucket_name": "u--data",
                    "access": "readwrite",
                    "alias": "default",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "enforced_access": "read",
                }
            ],
        )

    client = _install_mock_httpx(monkeypatch, handler)
    items = client.list_bucket_keys(SecretStr("tok"), None)
    assert items[0].enforced_access == "read"


def test_set_account_plan_posts_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/plan"
        assert _json.loads(request.content) == {"plan": "ally"}
        return httpx.Response(200, json={"plan_name": "ally", "entitlements": {}})

    client = _install_mock_httpx(monkeypatch, handler)
    body = client.set_account_plan(SecretStr("tok"), "ally")
    assert body["plan_name"] == "ally"


def test_set_account_plan_ineligible_403_surfaces_server_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """An eligibility refusal raises ImbueCloudAccountError with the server's reason, not an auth error."""
    reason = "The 'ally' plan requires partner access (a paid-listed email)"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": reason})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudAccountError, match="partner access"):
        client.set_account_plan(SecretStr("tok"), "ally")


def test_admin_account_endpoints_use_admin_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/quota"):
            assert _json.loads(request.content) == {"entitlement": "max_buckets", "value": 60.0}
            return httpx.Response(200, json={"status": "updated", "entitlement": "max_buckets", "value": 60})
        if request.url.path.endswith("/plan"):
            return httpx.Response(200, json={"plan_name": "ally", "entitlements": {}})
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "email": "alice@imbue.com",
                "plan_name": "explorer",
                "entitlements": {
                    "max_remote_workspaces": 2,
                    "max_total_workspaces": 6,
                    "max_buckets": 5,
                    "max_total_bucket_bytes": 53687091200,
                    "monthly_llm_spend_usd": 0.0,
                    "max_active_synced_workspaces": 200,
                },
                "usage": {
                    "remote_workspaces": 0,
                    "total_workspaces": 0,
                    "buckets": 0,
                    "total_bucket_bytes": 0,
                    "llm_spend_usd_this_period": 0.0,
                    "llm_budget_resets_at": None,
                    "active_synced_workspaces": 0,
                },
                "suspended_at": "2026-08-22 00:00:00+00:00",
                "suspended_reason": "abuse",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    info = client.admin_get_account(SecretStr("adm"), "alice@imbue.com")
    assert info.plan_name == "explorer"
    # The operator view carries the suspension state (what `account show` renders).
    assert info.suspended_at == "2026-08-22 00:00:00+00:00"
    assert info.suspended_reason == "abuse"
    client.admin_set_account_plan(SecretStr("adm"), "alice@imbue.com", "ally")
    client.admin_set_account_quota(SecretStr("adm"), "alice@imbue.com", "max_buckets", 60)
    assert seen == [
        "/admin/accounts/alice@imbue.com",
        "/admin/accounts/alice@imbue.com/plan",
        "/admin/accounts/alice@imbue.com/quota",
    ]


def test_admin_account_path_percent_encodes_reserved_email_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    """An email with URL-reserved characters ('#', '?') stays one intact path segment.

    Without percent-encoding, '?' would start a query string and '#' a
    fragment, silently addressing the wrong path.
    """
    email = "al#i?ce@imbue.com"
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"plan_name": "ally", "entitlements": {}})

    client = _install_mock_httpx(monkeypatch, handler)
    client.admin_set_account_plan(SecretStr("adm"), email, "ally")
    assert len(seen) == 1
    # httpx exposes the percent-decoded path: the full email survives intact.
    assert seen[0].path == f"/admin/accounts/{email}/plan"
    assert seen[0].query == b""
    assert seen[0].fragment == ""


# -- Paid lists (admin-key authenticated) --


def test_list_paid_domains_parses_and_sends_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/paid/domains"
        assert request.url.params.get("paid_only") == "true"
        assert request.headers["authorization"] == "Bearer admin-key-xyz"
        return httpx.Response(
            200,
            json=[
                {"domain": "imbue.com", "is_paid": True, "created_at": "t0", "updated_at": "t1"},
            ],
        )

    client = _install_mock_httpx(monkeypatch, handler)
    entries = client.list_paid_domains(SecretStr("admin-key-xyz"), paid_only=True)
    assert len(entries) == 1
    assert entries[0].value == "imbue.com"
    assert entries[0].is_paid is True


def test_list_paid_emails_maps_email_key_to_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/paid/emails"
        assert request.url.params.get("paid_only") == "false"
        return httpx.Response(
            200,
            json=[{"email": "bob@gmail.com", "is_paid": False, "created_at": "t0", "updated_at": "t1"}],
        )

    client = _install_mock_httpx(monkeypatch, handler)
    entries = client.list_paid_emails(SecretStr("k"), paid_only=False)
    assert entries[0].value == "bob@gmail.com"
    assert entries[0].is_paid is False


def test_add_paid_domain_posts_value(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"status": "added", "domain": "imbue.com"})

    client = _install_mock_httpx(monkeypatch, handler)
    result = client.add_paid_domain(SecretStr("k"), "Imbue.com")
    assert seen["path"] == "/paid/domains/add"
    assert seen["body"] == {"value": "Imbue.com"}
    assert result == {"status": "added", "domain": "imbue.com"}


def test_remove_paid_email_posts_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/paid/emails/remove"
        assert _json.loads(request.content) == {"value": "bob@gmail.com"}
        return httpx.Response(200, json={"status": "removed", "email": "bob@gmail.com"})

    client = _install_mock_httpx(monkeypatch, handler)
    result = client.remove_paid_email(SecretStr("k"), "bob@gmail.com")
    assert result == {"status": "removed", "email": "bob@gmail.com"}


def test_paid_list_unauthenticated_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid admin API key"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudAuthError):
        client.list_paid_domains(SecretStr("wrong"), paid_only=False)


# -- Transient-transport retry (_send) --
#
# The connector is a scale-to-zero Modal app, so a call can fail at the transport
# layer (DNS / reset / connect-timeout) before any HTTP response. ``_send`` rides
# those out with a bounded retry and, on terminal failure, raises a clean domain
# error (never the raw httpx traceback). HTTP *status* errors are NOT transport
# errors and must not be retried. One helper installs a flaky ``httpx.get`` so the
# monkeypatch ratchet counts a single occurrence across these tests.


def _install_flaky_httpx_get(
    monkeypatch: pytest.MonkeyPatch,
    fail_times: int,
    handler,
) -> tuple[ImbueCloudConnectorClient, dict]:
    transport = httpx.MockTransport(handler)
    state = {"calls": 0}

    def _get(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise httpx.ConnectError("[Errno -2] Name or service not known")
        with httpx.Client(transport=transport) as inner:
            return inner.get(*args, **kwargs)

    monkeypatch.setattr(httpx, "get", _get)
    return ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com")), state


def test_send_retries_transient_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"shares": []})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=1, handler=handler)
    # One transport failure then a success: the retry rides it out and the call
    # returns normally rather than surfacing the blip.
    assert client.list_shares(SecretStr("tok")) == []
    assert state["calls"] == 2


def test_send_wraps_terminal_transport_error_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler is never reached: every attempt fails at the transport layer.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"shares": []})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=99, handler=handler)
    with pytest.raises(ImbueCloudShareError) as exc_info:
        client.list_shares(SecretStr("tok"))
    # Retried up to the cap, then a clean domain error -- no raw traceback leaks
    # into the message that routes surface to API callers.
    assert state["calls"] == 3
    message = str(exc_info.value)
    assert "could not reach the imbue_cloud connector" in message
    assert "Traceback" not in message


def test_send_does_not_retry_http_status_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=0, handler=handler)
    # A 5xx is a response, not a transport error: it surfaces immediately via
    # ``_check`` without any retry.
    with pytest.raises(ImbueCloudShareError):
        client.list_shares(SecretStr("tok"))
    assert state["calls"] == 1


def test_get_workspace_retries_transient_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_workspace is the start-poll target (one GET every few seconds for up
    to 20 minutes): one transport blip must be retried, not abort the wait."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workspaces/00000000-0000-0000-0000-000000000042"
        return httpx.Response(200, json=_workspace_entry("running"))

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=1, handler=handler)
    workspace = client.get_workspace(SecretStr("tok"), "00000000-0000-0000-0000-000000000042")
    assert workspace.status == WorkspaceStatus.RUNNING
    assert state["calls"] == 2


def test_list_hosts_retries_transient_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The discovery read behind `mngr list`/`mngr create` must ride out a
    transport blip instead of surfacing "could not reach Imbue Cloud"."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/hosts"
        return httpx.Response(200, json={"hosts": []})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=1, handler=handler)
    assert client.list_hosts(SecretStr("tok")) == []
    assert state["calls"] == 2


def test_list_workspaces_retries_transient_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workspaces"
        return httpx.Response(200, json=[_workspace_entry("running")])

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=1, handler=handler)
    workspaces = client.list_workspaces(SecretStr("tok"))
    assert [workspace.status for workspace in workspaces] == [WorkspaceStatus.RUNNING]
    assert state["calls"] == 2


def test_list_hosts_exhausted_retries_raise_the_typed_unreachable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal transport failure on the listings surfaces as ImbueCloudUnreachableError,
    the type the provider maps back to ProviderUnavailableError (the user-facing
    "could not reach Imbue Cloud" card)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hosts": []})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=99, handler=handler)
    with pytest.raises(ImbueCloudUnreachableError) as exc_info:
        client.list_hosts(SecretStr("tok"))
    assert state["calls"] == 3
    assert "could not reach the imbue_cloud connector" in str(exc_info.value)


def test_list_hosts_does_not_retry_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 is a response, not a transport failure: fail fast with the auth type,
    exactly one request on the wire."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no token"})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=0, handler=handler)
    with pytest.raises(ImbueCloudAuthError):
        client.list_hosts(SecretStr("tok"))
    assert state["calls"] == 1


def test_list_workspaces_does_not_retry_the_old_connector_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old-connector fallback signal is status-based, so it must keep failing
    fast (one request) and keep its type -- callers use it to fall back to /hosts."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=0, handler=handler)
    with pytest.raises(WorkspacesEndpointUnavailableError):
        client.list_workspaces(SecretStr("tok"))
    assert state["calls"] == 1


# -- Modal 303 long-request redirects --
#
# The connector is a Modal web function: a synchronous request that runs long
# is answered with ``303 See Other`` pointing at an attempt-token URL the
# client must GET to fetch the eventual result. Every call path must follow
# it, or a slow-but-successful operation reads as a failure.


def test_release_host_follows_modal_303_redirect_to_result(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/hosts/db-1/release":
            assert request.method == "POST"
            return httpx.Response(303, headers={"Location": "/attempts/tok-303"})
        assert request.url.path == "/attempts/tok-303"
        assert request.method == "GET"
        return httpx.Response(200, json={"status": "released"})

    client = _install_mock_httpx(monkeypatch, handler)
    # A slow release that Modal parked behind an attempt URL still reads as
    # success -- before follow_redirects the bare 303 raised here.
    client.release_host(SecretStr("tok"), "db-1")
    assert seen_paths == ["/hosts/db-1/release", "/attempts/tok-303"]


def test_send_follows_modal_303_redirect_to_result() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/workspaces/00000000-0000-0000-0000-000000000042":
            return httpx.Response(303, headers={"Location": "/attempts/tok-303"})
        assert request.url.path == "/attempts/tok-303"
        assert request.method == "GET"
        return httpx.Response(200, json=_workspace_entry("running"))

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    workspace = client.get_workspace(SecretStr("tok"), "00000000-0000-0000-0000-000000000042")
    assert workspace.status == WorkspaceStatus.RUNNING
    assert seen_paths == ["/workspaces/00000000-0000-0000-0000-000000000042", "/attempts/tok-303"]


def test_every_module_level_httpx_call_in_client_follows_redirects() -> None:
    """The client mixes ``_send``-routed calls with direct module-level httpx
    calls, so "every connector call follows redirects" holds only if each
    direct call site carries the flag itself. A new endpoint written in the
    direct-call style without it would silently reintroduce the 303 bug for
    that endpoint, so pin the invariant over the module source."""
    tree = ast.parse(inspect.getsource(connector_client_module))
    lines_missing_follow_redirects: list[int] = []
    checked_call_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_module_level_httpx_verb = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
            and func.attr in ("get", "post", "put", "delete")
        )
        if not is_module_level_httpx_verb:
            continue
        checked_call_count += 1
        if "follow_redirects" not in {keyword.arg for keyword in node.keywords}:
            lines_missing_follow_redirects.append(node.lineno)
    # A floor well below the current count, purely to prove the scan matched
    # real call sites rather than passing vacuously after a refactor.
    assert checked_call_count >= 5
    assert lines_missing_follow_redirects == []


# -- Workspace sync methods --


def _sync_record_json(host_id: str = "host-1", revision: int = 1) -> dict[str, object]:
    return {
        "host_id": host_id,
        "agent_id": "agent-1",
        "display_name": "ws",
        "color": None,
        "provider_kind": "lima",
        "hosting_device_id": "device-1",
        "device_label": "laptop",
        "state": "active",
        "restored_from_host_id": None,
        "encrypted_secrets": None,
        "revision": revision,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_list_sync_records_parses_records(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sync/records"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"records": [_sync_record_json()]})

    client = _install_mock_httpx(monkeypatch, handler)
    records = client.list_sync_records(SecretStr("tok"))
    assert len(records) == 1
    assert records[0].host_id == "host-1"
    assert records[0].state == "active"
    assert records[0].destroyed_at is None


def test_list_sync_records_keeps_the_server_destroyed_at_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stamp must survive the strict (extra="forbid") transport model so
    # minds can age destroyed workspaces' backups against the server's clock.
    tombstone = {**_sync_record_json(), "state": "destroyed", "destroyed_at": "2026-07-01T00:00:00+00:00"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": [tombstone]})

    client = _install_mock_httpx(monkeypatch, handler)
    records = client.list_sync_records(SecretStr("tok"))
    assert records[0].destroyed_at == "2026-07-01T00:00:00+00:00"


def test_put_sync_record_uses_the_workspace_keyed_route(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/sync/records/by-workspace/agent-1"
        body = _json.loads(request.content)
        assert body["revision"] == 1
        return httpx.Response(200, json=_sync_record_json())

    client = _install_mock_httpx(monkeypatch, handler)
    stored = client.put_sync_record(SecretStr("tok"), SyncWorkspaceRecord.model_validate(_sync_record_json()))
    assert stored.revision == 1


def test_put_sync_record_falls_back_to_the_host_route_on_an_older_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.startswith("/sync/records/by-workspace/"):
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=_sync_record_json())

    client = _install_mock_httpx(monkeypatch, handler)
    stored = client.put_sync_record(SecretStr("tok"), SyncWorkspaceRecord.model_validate(_sync_record_json()))
    assert stored.revision == 1
    assert seen_paths == ["/sync/records/by-workspace/agent-1", "/sync/records/host-1"]


def test_delete_sync_record_by_workspace_uses_the_workspace_keyed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "deleted"})

    client = _install_mock_httpx(monkeypatch, handler)
    client.delete_sync_record_by_workspace(SecretStr("tok"), "agent-1")
    assert calls == [("DELETE", "/sync/records/by-workspace/agent-1")]


def test_delete_sync_record_by_workspace_falls_back_via_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "DELETE" and request.url.path.startswith("/sync/records/by-workspace/"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "GET" and request.url.path == "/sync/records":
            return httpx.Response(200, json={"records": [_sync_record_json()]})
        return httpx.Response(200, json={"status": "deleted"})

    client = _install_mock_httpx(monkeypatch, handler)
    client.delete_sync_record_by_workspace(SecretStr("tok"), "agent-1")
    assert calls == [
        ("DELETE", "/sync/records/by-workspace/agent-1"),
        ("GET", "/sync/records"),
        ("DELETE", "/sync/records/host-1"),
    ]


def test_put_sync_record_conflict_carries_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"detail": {"message": "revision conflict", "stored": _sync_record_json(revision=4)}}
        )

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudSyncConflictError) as exc_info:
        client.put_sync_record(SecretStr("tok"), SyncWorkspaceRecord.model_validate(_sync_record_json()))
    assert exc_info.value.stored_record is not None
    assert exc_info.value.stored_record["revision"] == 4


def test_put_sync_record_agent_conflict_has_no_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"message": "another ACTIVE record already exists"}})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudSyncConflictError) as exc_info:
        client.put_sync_record(SecretStr("tok"), SyncWorkspaceRecord.model_validate(_sync_record_json()))
    assert exc_info.value.stored_record is None


def test_scrub_sync_secrets_returns_count(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sync/scrub-secrets"
        return httpx.Response(200, json={"scrubbed": 3})

    client = _install_mock_httpx(monkeypatch, handler)
    assert client.scrub_sync_secrets(SecretStr("tok")) == 3


def test_get_key_bundle_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No key bundle stored for this account"})

    client = _install_mock_httpx(monkeypatch, handler)
    assert client.get_key_bundle(SecretStr("tok")) is None


def test_key_bundle_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_json = {
        "kdf_salt": "c2FsdHNhbHRzYWx0c2FsdA==",
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": "d3JhcHBlZA==",
        "key_epoch": 1,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            body = _json.loads(request.content)
            assert body["wrapped_dek"] == bundle_json["wrapped_dek"]
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET":
            return httpx.Response(200, json=bundle_json)
        return httpx.Response(200, json={"status": "deleted"})

    client = _install_mock_httpx(monkeypatch, handler)
    client.put_key_bundle(SecretStr("tok"), SyncKeyBundle.model_validate(bundle_json))
    fetched = client.get_key_bundle(SecretStr("tok"))
    assert fetched is not None
    assert fetched.key_epoch == 1
    client.delete_key_bundle(SecretStr("tok"))


def test_sync_records_auth_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid token"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudAuthError):
        client.list_sync_records(SecretStr("bad"))


# ---------------------------------------------------------------------------
# create_litellm_key_rotating_on_exists
# ---------------------------------------------------------------------------


class _RotationScriptedConnectorClient(ImbueCloudConnectorClient):
    """Concrete fake for the rotation helper: scripted create failures, canned key list."""

    existing_keys: list[LiteLLMKeyInfo] = Field(default_factory=list)
    alias_exists_failure_count: int = Field(
        default=0, description="How many leading creates fail on the unique-alias constraint"
    )
    create_call_count: int = Field(default=0, description="Creates attempted so far")
    list_call_count: int = Field(default=0, description="List calls so far")
    deleted_key_ids: list[str] = Field(default_factory=list)

    def create_litellm_key(
        self,
        access_token: SecretStr,
        key_alias: str | None,
        max_budget: float | None,
        budget_duration: str | None,
        metadata: dict[str, str] | None,
    ) -> LiteLLMKeyMaterial:
        self.create_call_count += 1
        if self.create_call_count <= self.alias_exists_failure_count:
            raise ImbueCloudKeyError(
                f"Key creation failed (400): LiteLLM error: Key with alias '{key_alias}' already exists. "
                "Unique key aliases across all keys are required."
            )
        return LiteLLMKeyMaterial(key=SecretStr("sk-rotated"), base_url=AnyUrl("https://llm.example.com"))

    def list_litellm_keys(self, access_token: SecretStr) -> list[LiteLLMKeyInfo]:
        self.list_call_count += 1
        return list(self.existing_keys)

    def delete_litellm_key(self, access_token: SecretStr, key_id: str) -> None:
        self.deleted_key_ids.append(key_id)


def test_rotating_create_deletes_only_the_matching_alias_and_recreates() -> None:
    client = _RotationScriptedConnectorClient(
        base_url=AnyUrl("https://example.com"),
        alias_exists_failure_count=1,
        existing_keys=[
            LiteLLMKeyInfo(token="hash-other", key_alias="workspace-host-other"),
            LiteLLMKeyInfo(token="hash-abc", key_alias="workspace-host-abc"),
        ],
    )

    material = create_litellm_key_rotating_on_exists(
        client=client,
        access_token=SecretStr("tok"),
        key_alias="workspace-host-abc",
        max_budget=100.0,
        budget_duration="1d",
        metadata={"workspace_host_id": "host-abc"},
    )

    assert material.key.get_secret_value() == "sk-rotated"
    assert client.deleted_key_ids == ["hash-abc"]
    assert client.create_call_count == 2
    assert client.list_call_count == 1


def test_rotating_create_returns_directly_when_the_alias_is_free() -> None:
    client = _RotationScriptedConnectorClient(base_url=AnyUrl("https://example.com"))

    material = create_litellm_key_rotating_on_exists(
        client=client,
        access_token=SecretStr("tok"),
        key_alias="workspace-host-abc",
        max_budget=None,
        budget_duration=None,
        metadata=None,
    )

    assert material.key.get_secret_value() == "sk-rotated"
    assert client.create_call_count == 1
    assert client.list_call_count == 0
    assert client.deleted_key_ids == []


class _AlwaysFailingCreateConnectorClient(_RotationScriptedConnectorClient):
    """Fake whose create always fails with a non-alias error (transport, 5xx, ...)."""

    def create_litellm_key(
        self,
        access_token: SecretStr,
        key_alias: str | None,
        max_budget: float | None,
        budget_duration: str | None,
        metadata: dict[str, str] | None,
    ) -> LiteLLMKeyMaterial:
        self.create_call_count += 1
        raise ImbueCloudKeyError("Key creation HTTP request failed: connection reset")


def test_rotating_create_propagates_unrelated_create_errors_without_rotating() -> None:
    client = _AlwaysFailingCreateConnectorClient(base_url=AnyUrl("https://example.com"))

    with pytest.raises(ImbueCloudKeyError, match="HTTP request failed"):
        create_litellm_key_rotating_on_exists(
            client=client,
            access_token=SecretStr("tok"),
            key_alias="workspace-host-abc",
            max_budget=None,
            budget_duration=None,
            metadata=None,
        )

    assert client.list_call_count == 0
    assert client.deleted_key_ids == []


def test_rotating_create_errors_when_no_listable_key_matches_the_alias() -> None:
    # The alias is taken but not listable under this account (e.g. it belongs
    # to another account): fail with a clear message, delete nothing.
    client = _RotationScriptedConnectorClient(
        base_url=AnyUrl("https://example.com"),
        alias_exists_failure_count=1,
        existing_keys=[LiteLLMKeyInfo(token="hash-other", key_alias="workspace-host-other")],
    )

    with pytest.raises(ImbueCloudKeyError, match="already taken"):
        create_litellm_key_rotating_on_exists(
            client=client,
            access_token=SecretStr("tok"),
            key_alias="workspace-host-abc",
            max_budget=None,
            budget_duration=None,
            metadata=None,
        )

    assert client.deleted_key_ids == []
    assert client.create_call_count == 1


# ---------------------------------------------------------------------------
# Shares (self-hosted relays)
# ---------------------------------------------------------------------------

_SHARE_HOST_ID = "host-" + "a" * 32
_SHARE_DOMAIN = _SHARE_HOST_ID + "." + "b" * 32 + ".us1.imbueminds.com"


def test_create_share_parses_token_and_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares"
        assert _json.loads(request.content) == {"host_id": _SHARE_HOST_ID}
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "host_id": _SHARE_HOST_ID,
                "workspace_domain": _SHARE_DOMAIN,
                "region": "us1",
                "relay_endpoints": [{"relay_id": "relay-" + "1" * 16, "endpoint": "relay-us1.infra.imbue.com:7000"}],
                "relay_token": "secret-relay-token",
                "chrome_origin": "https://minds.example.com",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    info = client.create_share(SecretStr("tok"), _SHARE_HOST_ID)

    assert info.workspace_domain == _SHARE_DOMAIN
    assert info.region == "us1"
    assert info.state == "active"
    assert [entry.endpoint for entry in info.relay_endpoints] == ["relay-us1.infra.imbue.com:7000"]
    assert info.relay_endpoints[0].relay_id == "relay-" + "1" * 16
    assert info.relay_token is not None
    assert info.relay_token.get_secret_value() == "secret-relay-token"
    assert info.chrome_origin == "https://minds.example.com"


def test_create_share_defaults_chrome_origin_to_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A connector that predates the field (or a tier with no hosted chrome)
    # sends nothing; callers use None to fall back to the connector origin.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares"
        return httpx.Response(
            200,
            json={
                "host_id": _SHARE_HOST_ID,
                "workspace_domain": _SHARE_DOMAIN,
                "region": "us1",
                "relay_endpoints": [],
                "relay_token": "secret-relay-token",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    info = client.create_share(SecretStr("tok"), _SHARE_HOST_ID)

    assert info.chrome_origin is None


def test_create_share_sends_preferred_region(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares"
        assert _json.loads(request.content) == {"host_id": _SHARE_HOST_ID, "preferred_region": "us2"}
        return httpx.Response(
            200,
            json={
                "host_id": _SHARE_HOST_ID,
                "workspace_domain": _SHARE_DOMAIN,
                "region": "us2",
                "relay_endpoints": [{"relay_id": "relay-" + "2" * 16, "endpoint": "relay-us2.infra.imbue.com:7000"}],
                "relay_token": "secret-relay-token",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    info = client.create_share(SecretStr("tok"), _SHARE_HOST_ID, preferred_region="us2")

    assert info.region == "us2"


def test_create_share_sends_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = "agent-" + "c" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares"
        # The workspace id must ride the body: dropping it silently downgrades
        # the share to the legacy host-keyed flow (host-id-led domain that
        # does not follow the workspace across machines).
        assert _json.loads(request.content) == {"host_id": _SHARE_HOST_ID, "workspace_id": workspace_id}
        return httpx.Response(
            200,
            json={
                "host_id": _SHARE_HOST_ID,
                "workspace_id": workspace_id,
                "workspace_domain": _SHARE_DOMAIN,
                "region": "us1",
                "relay_endpoints": [{"relay_id": "relay-" + "1" * 16, "endpoint": "relay-us1.infra.imbue.com:7000"}],
                "relay_token": "secret-relay-token",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    info = client.create_share(SecretStr("tok"), _SHARE_HOST_ID, workspace_id=workspace_id)

    assert info.workspace_domain == _SHARE_DOMAIN


def test_list_share_relays_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares/relays"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "relays": {
                    "us1": ["relay-us1.infra.imbue.com:7000", "relay-us1b.infra.imbue.com:7000"],
                    "us2": ["relay-us2.infra.imbue.com:7000"],
                },
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    relay_map = client.list_share_relays(SecretStr("tok"))

    assert relay_map.relay_endpoints_by_region == {
        "us1": ("relay-us1.infra.imbue.com:7000", "relay-us1b.infra.imbue.com:7000"),
        "us2": ("relay-us2.infra.imbue.com:7000",),
    }


@pytest.mark.parametrize(
    "malformed_body",
    [
        {"unexpected": True},
        # The old region -> single-endpoint shape: a non-list per-region value.
        {"relays": {"us1": "relay-us1.infra.imbue.com:7000"}},
    ],
)
def test_list_share_relays_raises_on_a_malformed_body(
    monkeypatch: pytest.MonkeyPatch, malformed_body: dict[str, object]
) -> None:
    # A malformed body must raise rather than degrade to an empty or partial
    # map (which would silently disable latency-based region picking).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed_body)

    client = _install_mock_httpx(monkeypatch, handler)

    with pytest.raises(ImbueCloudShareError, match="malformed relays response"):
        client.list_share_relays(SecretStr("tok"))


def test_admin_relay_endpoints_use_admin_paths_and_parse_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _relay_id = "relay-" + "1" * 16
    relay_row = {
        "relay_id": _relay_id,
        "region": "us1",
        "tunnel_endpoint": "198.51.100.7:7000",
        "ip_address": "198.51.100.7",
        "instance_name": "share-relay-staging-us1-1",
        "is_active": True,
        "health": "healthy",
        "consecutive_probe_failures": 0,
    }
    seen: list[tuple[str, str]] = []
    posted_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer adm"
        if request.method == "GET":
            return httpx.Response(200, json={"relays": [relay_row]})
        if request.method == "POST":
            posted_bodies.append(_json.loads(request.content))
            return httpx.Response(200, json=relay_row)
        return httpx.Response(200, json={"relay_id": _relay_id, "is_active": False})

    client = _install_mock_httpx(monkeypatch, handler)

    listed = client.admin_list_relays(SecretStr("adm"))
    assert [row.relay_id for row in listed] == [_relay_id]
    assert listed[0].health == "healthy"

    # Without a relay id, the body omits the key (the connector mints one);
    # with an id, it re-registers/revives that relay in place.
    registered = client.admin_register_relay(
        SecretStr("adm"),
        relay_id=None,
        region="us1",
        tunnel_endpoint="198.51.100.7:7000",
        ip_address="198.51.100.7",
        instance_name="share-relay-staging-us1-1",
    )
    assert registered.relay_id == _relay_id
    client.admin_register_relay(
        SecretStr("adm"),
        relay_id=_relay_id,
        region="us1",
        tunnel_endpoint="198.51.100.7:7000",
        ip_address="198.51.100.7",
        instance_name="share-relay-staging-us1-1",
    )
    assert "relay_id" not in posted_bodies[0]
    assert posted_bodies[1]["relay_id"] == _relay_id

    retired = client.admin_retire_relay(SecretStr("adm"), _relay_id)
    assert retired == {"relay_id": _relay_id, "is_active": False}

    assert seen == [
        ("GET", "/admin/relays"),
        ("POST", "/admin/relays"),
        ("POST", "/admin/relays"),
        ("DELETE", f"/admin/relays/{_relay_id}"),
    ]


def test_create_share_quota_surfaces_as_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "quota_exceeded",
                    "entitlement": "max_shared_workspaces",
                    "limit": 50,
                    "current": 50,
                    "message": "too many",
                }
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    with pytest.raises(ImbueCloudQuotaExceededError):
        client.create_share(SecretStr("tok"), _SHARE_HOST_ID)


def test_create_share_server_error_raises_share_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "sharing not configured"})

    client = _install_mock_httpx(monkeypatch, handler)

    with pytest.raises(ImbueCloudShareError):
        client.create_share(SecretStr("tok"), _SHARE_HOST_ID)


def test_get_share_status_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No share found"})

    client = _install_mock_httpx(monkeypatch, handler)

    assert client.get_share_status(SecretStr("tok"), _SHARE_HOST_ID) is None


def test_get_share_status_parses_status_document(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/shares/{_SHARE_HOST_ID}/status"
        return httpx.Response(
            200,
            json={
                "host_id": _SHARE_HOST_ID,
                "workspace_domain": _SHARE_DOMAIN,
                "region": "us1",
                "state": "active",
                "relay_endpoints": [{"relay_id": "relay-" + "1" * 16, "endpoint": "relay-us1.infra.imbue.com:7000"}],
                "relays": [{"relay_id": "relay-" + "1" * 16, "last_login_at": "2026-07-29 01:02:03+00:00"}],
                "last_tunnel_login_at": "2026-07-29 01:02:03+00:00",
                "cert_not_after": "2026-10-01 00:00:00+00:00",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    info = client.get_share_status(SecretStr("tok"), _SHARE_HOST_ID)

    assert info is not None
    assert info.state == "active"
    assert info.last_tunnel_login_at == "2026-07-29 01:02:03+00:00"
    # The per-relay login stamps identify WHICH relay the tunnel reached.
    assert [(entry.relay_id, entry.last_login_at) for entry in info.relays] == [
        ("relay-" + "1" * 16, "2026-07-29 01:02:03+00:00")
    ]
    assert info.cert_not_after == "2026-10-01 00:00:00+00:00"
    assert info.relay_token is None


def test_delete_share_hits_the_share_route(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"host_id": _SHARE_HOST_ID, "state": "inactive"})

    client = _install_mock_httpx(monkeypatch, handler)

    client.delete_share(SecretStr("tok"), _SHARE_HOST_ID)

    assert seen_paths == [f"/shares/{_SHARE_HOST_ID}"]


def test_list_shares_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shares"
        return httpx.Response(
            200,
            json={
                "shares": [
                    {
                        "host_id": _SHARE_HOST_ID,
                        "workspace_domain": _SHARE_DOMAIN,
                        "region": "us1",
                        "state": "inactive",
                    }
                ]
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)

    items = client.list_shares(SecretStr("tok"))

    assert len(items) == 1
    assert items[0].host_id == _SHARE_HOST_ID
    assert items[0].state == "inactive"


# ----------------------------------------------------------------------
# Browser-login support probe + device-token exchange
# ----------------------------------------------------------------------


def _make_transport_client(handler) -> ImbueCloudConnectorClient:
    """Client using the injected-transport seam (no module-level httpx patching)."""
    return ImbueCloudConnectorClient(
        base_url=AnyUrl("https://example.com"),
        transport=httpx.MockTransport(handler),
    )


def test_supports_browser_login_true_when_accounts_config_is_served() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/api/config"
        return httpx.Response(200, json={"turnstile_site_key": "", "google_enabled": False})

    assert _make_transport_client(handler).supports_browser_login() is True


def test_supports_browser_login_false_when_the_connector_is_too_old() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    assert _make_transport_client(handler).supports_browser_login() is False


def test_auth_device_token_maps_404_to_a_too_old_connector_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = _make_transport_client(handler)
    with pytest.raises(ImbueCloudAuthError, match="minds-admin env deploy"):
        client.auth_device_token(code="c", code_verifier="v", redirect_uri="http://127.0.0.1:1/callback")


def test_auth_device_token_parses_a_successful_exchange() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body == {"code": "c", "code_verifier": "v", "redirect_uri": "http://127.0.0.1:1/callback"}
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "user": {"user_id": "u-1", "email": "a@example.com", "display_name": None},
                "tokens": {"access_token": "at", "refresh_token": "rt"},
            },
        )

    response = _make_transport_client(handler).auth_device_token(
        code="c", code_verifier="v", redirect_uri="http://127.0.0.1:1/callback"
    )
    assert response.status == "OK"
    assert response.tokens == {"access_token": "at", "refresh_token": "rt"}


def test_auth_revoke_current_session_treats_success_and_401_as_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """200/204/401 all count as revoked (401 = already revoked); no fallback call is made."""
    for status_code in (200, 204, 401):
        calls: list[str] = []

        def handler(request: httpx.Request, calls: list[str] = calls, status_code: int = status_code):
            calls.append(request.url.path)
            return httpx.Response(status_code)

        client = _install_mock_httpx(monkeypatch, handler)
        client.auth_revoke_current_session(SecretStr("tok"))
        assert calls == ["/auth/session/revoke-current"]


def test_auth_revoke_current_session_falls_back_to_revoke_all_on_an_old_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector without the device-scoped route (404) gets the revoke-all fallback, so the token never stays live."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/auth/session/revoke-current":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(204)

    client = _install_mock_httpx(monkeypatch, handler)
    client.auth_revoke_current_session(SecretStr("tok"))
    assert calls == ["/auth/session/revoke-current", "/auth/session/revoke"]


def test_auth_revoke_current_session_raises_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudAuthError, match="Revoke failed"):
        client.auth_revoke_current_session(SecretStr("tok"))


def test_set_account_plan_maps_structured_verification_403_to_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/plan"
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "email_not_verified",
                    "email": "alice@example.com",
                    "message": "This action requires a verified email address (alice@example.com).",
                }
            },
        )

    client = _make_transport_client(handler)
    with pytest.raises(ImbueCloudEmailNotVerifiedError) as exc_info:
        client.set_account_plan(SecretStr("tok"), "ally")
    assert exc_info.value.email == "alice@example.com"


def _workspace_entry(status: str = "stopped") -> dict:
    return {
        "host_db_id": "00000000-0000-0000-0000-000000000042",
        "status": status,
        "vps_address": None if status == "stopped" else "10.0.0.9",
        "ssh_port": None if status == "stopped" else 22000,
        "ssh_user": "root",
        "container_ssh_port": None if status == "stopped" else 22001,
        "agent_id": "agent-abc",
        "host_id": "host-" + "a" * 32,
        "host_name": "my-workspace",
        "attributes": {"cpus": 2},
        "leased_at": "2026-01-01T00:00:00+00:00",
        "stop_requested_at": "2026-01-02T00:00:00+00:00",
        "stopped_at": "2026-01-02T00:20:00+00:00" if status == "stopped" else None,
        "transition_error": None,
    }


def test_list_workspaces_parses_all_lifecycle_states(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workspaces"
        return httpx.Response(200, json=[_workspace_entry("running"), _workspace_entry("stopped")])

    client = _install_mock_httpx(monkeypatch, handler)
    workspaces = client.list_workspaces(SecretStr("tok"))

    assert [w.status for w in workspaces] == [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED]
    assert workspaces[0].vps_address == "10.0.0.9"
    assert workspaces[1].vps_address is None
    assert workspaces[1].container_ssh_port is None


def test_list_workspaces_raises_unavailable_on_old_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(WorkspacesEndpointUnavailableError):
        client.list_workspaces(SecretStr("tok"))


def test_stop_workspace_404_with_specific_detail_is_not_the_old_connector_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A modern connector's own 404 (e.g. the row was released concurrently)
    carries a specific detail and must surface as a real error, not be
    misdiagnosed as a connector without the /workspaces endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No such workspace"})

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudConnectorError) as excinfo:
        client.stop_workspace(SecretStr("tok"), "00000000-0000-0000-0000-000000000042")
    assert not isinstance(excinfo.value, WorkspacesEndpointUnavailableError)
    assert "No such workspace" in str(excinfo.value)


def test_stop_workspace_returns_wire_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workspaces/00000000-0000-0000-0000-000000000042/stop"
        return httpx.Response(202, json={"host_db_id": "00000000-0000-0000-0000-000000000042", "status": "stopping"})

    client = _install_mock_httpx(monkeypatch, handler)
    status = client.stop_workspace(SecretStr("tok"), "00000000-0000-0000-0000-000000000042")

    assert status == WorkspaceStatus.STOPPING


def test_start_workspace_surfaces_quota_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "quota_exceeded",
                    "entitlement": "max_remote_workspaces",
                    "limit": 2,
                    "current": 2,
                    "message": "over quota",
                }
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudQuotaExceededError):
        client.start_workspace(SecretStr("tok"), "00000000-0000-0000-0000-000000000042")


def test_admin_abandon_workspace_posts_reason_with_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"host_db_id": "00000000-0000-0000-0000-000000000042", "status": "crashed"})

    client = _install_mock_httpx(monkeypatch, handler)
    client.admin_abandon_workspace(SecretStr("adminkey"), "00000000-0000-0000-0000-000000000042", "box died")

    assert seen["path"] == "/admin/workspaces/00000000-0000-0000-0000-000000000042/abandon"
    assert seen["auth"] == "Bearer adminkey"
    assert seen["body"] == {"reason": "box died"}


def test_admin_release_workspace_posts_with_admin_key_and_returns_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "released"})

    client = _install_mock_httpx(monkeypatch, handler)
    status = client.admin_release_workspace(SecretStr("adminkey"), "00000000-0000-0000-0000-000000000043")

    assert status == "released"
    assert seen["path"] == "/admin/workspaces/00000000-0000-0000-0000-000000000043/release"
    assert seen["auth"] == "Bearer adminkey"


def test_admin_run_lease_record_sweep_passes_dry_run_and_grace_as_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "completed", "result": {"dry_run": True}})

    client = _install_mock_httpx(monkeypatch, handler)
    result = client.admin_run_lease_record_sweep(SecretStr("adminkey"), dry_run=True, grace_seconds=0.0)

    assert result["result"] == {"dry_run": True}
    assert seen["path"] == "/admin/sweep/lease-records"
    assert seen["query"] == {"dry_run": "1", "grace_seconds": "0.0"}
    assert seen["auth"] == "Bearer adminkey"


def test_every_request_carries_the_client_identification_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, json=[])

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    client.list_hosts(SecretStr("token"))
    client.auth_forgot_password("a@b.com")

    assert len(seen_headers) == 2
    for headers in seen_headers:
        identifier = headers.get(CLIENT_ID_HEADER)
        assert identifier is not None and "imbue-cloud-plugin/" in identifier
        assert headers.get("user-agent") == identifier


def test_http_426_raises_the_typed_client_too_old_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            426,
            json={
                "detail": {
                    "code": "client_too_old",
                    "min_version": "0.4.0",
                    "sunset_date": "2026-10-01",
                    "message": "This app version is no longer supported; please update it.",
                }
            },
        )

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))

    with pytest.raises(ImbueCloudClientTooOldError) as exc_info:
        client.get_account(SecretStr("token"))
    assert exc_info.value.min_version == "0.4.0"
    assert exc_info.value.sunset_date == "2026-10-01"


def test_record_push_maps_the_format_conflict_to_its_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": {"code": "record_format_too_new", "message": "update the app to modify it", "stored": {}}},
        )

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    record = SyncWorkspaceRecord(
        host_id="host-1", agent_id="agent-1", provider_kind="lima", state="active", revision=2
    )

    with pytest.raises(ImbueCloudRecordFormatTooNewError) as exc_info:
        client.put_sync_record(SecretStr("token"), record)
    # The typed error carries the connector's human message alone, not the
    # repr of the whole detail dict (stored row included).
    assert str(exc_info.value) == "update the app to modify it"


def test_listing_with_every_entry_unparseable_raises_instead_of_reporting_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"unrelated": 1}, {"unrelated": 2}])

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))

    with pytest.raises(ImbueCloudConnectorError, match="refusing to report an empty listing"):
        client.list_workspaces(SecretStr("token"))


def test_workspace_with_unrecognized_status_coerces_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {
        "host_db_id": "00000000-0000-0000-0000-000000000001",
        "status": "migrating",
        "agent_id": "agent-1",
        "host_id": "host-1",
        "host_name": "ws",
        "added_by_a_newer_server": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[entry])

    _install_fake_transport(monkeypatch, handler)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))

    workspaces = client.list_workspaces(SecretStr("token"))

    assert len(workspaces) == 1
    assert workspaces[0].status is WorkspaceStatus.UNKNOWN


def test_admin_suspension_endpoints_hit_the_right_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"status": "ok", "steps": {}})

    client = _install_mock_httpx(monkeypatch, handler)
    client.admin_revoke_sessions(SecretStr("adm"), "alice@imbue.com")
    client.admin_suspend_account(SecretStr("adm"), "alice@imbue.com", "abuse", block_storage=True)
    client.admin_unsuspend_account(SecretStr("adm"), "alice@imbue.com")
    client.admin_stop_workspace(SecretStr("adm"), "11111111-2222-3333-4444-555566667777")
    assert seen == [
        ("POST", "/admin/accounts/alice@imbue.com/revoke-sessions", None),
        ("POST", "/admin/accounts/alice@imbue.com/suspend", {"reason": "abuse", "block_storage": True}),
        ("POST", "/admin/accounts/alice@imbue.com/unsuspend", None),
        ("POST", "/admin/workspaces/11111111-2222-3333-4444-555566667777/stop", None),
    ]


def test_account_suspended_403_raises_the_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "account_suspended",
                    "message": "This account is suspended. Contact support@imbue.com.",
                }
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    with pytest.raises(ImbueCloudAccountSuspendedError, match="support@imbue.com"):
        client.auth_device_token("code-1", "verifier-1", "http://127.0.0.1:1234/callback")
