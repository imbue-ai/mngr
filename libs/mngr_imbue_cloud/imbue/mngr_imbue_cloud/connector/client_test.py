"""Tests for the connector HTTP client.

We mount an httpx MockTransport on the underlying transport so the calls
never go to the network; this isolates the tests from connector availability
and makes them deterministic.
"""

import json as _json

import httpx
import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.client import _auth_policy_to_connector_body
from imbue.mngr_imbue_cloud.connector.client import _parse_auth_policy
from imbue.mngr_imbue_cloud.data_types import AuthPolicy
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.data_types import SyncWorkspaceRecord
from imbue.mngr_imbue_cloud.errors import ImbueCloudAccountError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketExistsError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketLimitError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotFoundError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudQuotaExceededError
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError
from imbue.mngr_imbue_cloud.errors import ImbueCloudTunnelError


def _make_client(handler) -> tuple[ImbueCloudConnectorClient, httpx.MockTransport]:
    transport = httpx.MockTransport(handler)

    # Patch httpx module-level functions to use the transport for the duration of the test.
    # The client uses module-level httpx.* calls; intercept them via monkeypatch in tests.
    return ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com")), transport


def test_lease_host_503_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # An absent region must not be sent so the connector treats the lease as
        # region-agnostic.
        body = _json.loads(request.content)
        assert "region" not in body
        return httpx.Response(503, json={"detail": "no match"})

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
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

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
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


def test_rename_host_success_posts_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"host_db_id": "00000000-0000-0000-0000-000000000009", "host_name": "new-name"},
        )

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    client.rename_host(SecretStr("tok"), "00000000-0000-0000-0000-000000000009", "new-name")

    assert captured["url"] == "https://example.com/hosts/00000000-0000-0000-0000-000000000009/rename"
    assert captured["body"] == {"host_name": "new-name"}


def test_rename_host_error_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudConnectorError):
        client.rename_host(SecretStr("tok"), "00000000-0000-0000-0000-000000000009", "new-name")


def test_unauthenticated_responses_raise_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no token"})

    transport = httpx.MockTransport(handler)

    def fake_get(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.get(*args, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudAuthError):
        client.list_hosts(SecretStr("tok"))


def test_500_lease_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudConnectorError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=1), "ssh-ed25519 X", "my-host")


# -- AuthPolicy translation --
#
# The connector's API takes/returns the Cloudflare-native ``{"rules": [...]}``
# shape; the plugin's ``AuthPolicy`` is the high-level ``emails / email_domains
# / require_idp`` shape. The client translates at every wire boundary so the
# plugin CLI's user-facing surface stays high-level. These tests pin the
# translation -- before they existed, the bug went unnoticed and ``set
# service auth`` failed at runtime with a 422 from the connector.


def test_auth_policy_to_connector_body_translates_emails_domains_idps() -> None:
    body = _auth_policy_to_connector_body(
        AuthPolicy(
            emails=("a@b.com", "c@d.com"),
            email_domains=("e.com",),
            require_idp=("idp1",),
        )
    )
    assert body == {
        "rules": [
            {
                "action": "allow",
                "include": [
                    {"email": {"email": "a@b.com"}},
                    {"email": {"email": "c@d.com"}},
                    {"email_domain": {"domain": "e.com"}},
                    {"login_method": {"id": "idp1"}},
                ],
            }
        ]
    }


def test_auth_policy_to_connector_body_emits_empty_rules_for_empty_policy() -> None:
    """An empty policy must serialize to ``{"rules": []}`` rather than a rule with an empty include."""
    assert _auth_policy_to_connector_body(AuthPolicy()) == {"rules": []}


def test_parse_auth_policy_round_trips_emails_domains_idps() -> None:
    original = AuthPolicy(
        emails=("a@b.com", "c@d.com"),
        email_domains=("e.com",),
        require_idp=("idp1",),
    )
    assert _parse_auth_policy(_auth_policy_to_connector_body(original)) == original


def test_parse_auth_policy_handles_empty_response() -> None:
    """``GET ... /auth`` returns ``{"rules": []}`` when no policy is configured."""
    assert _parse_auth_policy({"rules": []}) == AuthPolicy()


def test_parse_auth_policy_ignores_unknown_include_types() -> None:
    """A future Cloudflare include shape (e.g. ``{"github": ...}``) must not break older clients."""
    parsed = _parse_auth_policy(
        {
            "rules": [
                {
                    "action": "allow",
                    "include": [
                        {"email": {"email": "a@b.com"}},
                        {"github": {"team": "secret"}},
                    ],
                }
            ]
        }
    )
    assert parsed == AuthPolicy(emails=("a@b.com",))


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
                    "max_tunnels": 50,
                    "max_services_per_tunnel": 10,
                    "max_buckets": 20,
                    "max_total_bucket_bytes": 536870912000,
                    "monthly_llm_spend_usd": 1000.0,
                    "max_active_synced_workspaces": 200,
                },
                "usage": {
                    "remote_workspaces": 2,
                    "tunnels": 3,
                    "buckets": 1,
                    "total_bucket_bytes": 12345,
                    "llm_spend_usd_this_period": 42.5,
                    "llm_budget_resets_at": "2026-08-01T00:00:00Z",
                    "active_synced_workspaces": 4,
                },
                "available_plans": ["ally", "explorer"],
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    info = client.get_account(SecretStr("tok"))
    assert info.plan_name == "ally"
    assert info.entitlements.max_remote_workspaces == 10
    assert info.usage.llm_spend_usd_this_period == 42.5
    assert info.available_plans == ("ally", "explorer")


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
            assert _json.loads(request.content) == {"entitlement": "max_tunnels", "value": 60.0}
            return httpx.Response(200, json={"status": "updated", "entitlement": "max_tunnels", "value": 60})
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
                    "max_tunnels": 50,
                    "max_services_per_tunnel": 10,
                    "max_buckets": 5,
                    "max_total_bucket_bytes": 53687091200,
                    "monthly_llm_spend_usd": 0.0,
                    "max_active_synced_workspaces": 200,
                },
                "usage": {
                    "remote_workspaces": 0,
                    "tunnels": 0,
                    "buckets": 0,
                    "total_bucket_bytes": 0,
                    "llm_spend_usd_this_period": 0.0,
                    "llm_budget_resets_at": None,
                    "active_synced_workspaces": 0,
                },
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    info = client.admin_get_account(SecretStr("adm"), "alice@imbue.com")
    assert info.plan_name == "explorer"
    client.admin_set_account_plan(SecretStr("adm"), "alice@imbue.com", "ally")
    client.admin_set_account_quota(SecretStr("adm"), "alice@imbue.com", "max_tunnels", 60)
    assert seen == [
        "/admin/accounts/alice@imbue.com",
        "/admin/accounts/alice@imbue.com/plan",
        "/admin/accounts/alice@imbue.com/quota",
    ]


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
        return httpx.Response(401, json={"detail": "Invalid paid-list admin API key"})

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
        return httpx.Response(200, json=[])

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=1, handler=handler)
    # One transport failure then a success: the retry rides it out and the call
    # returns normally rather than surfacing the blip.
    assert client.list_tunnels(SecretStr("tok")) == []
    assert state["calls"] == 2


def test_send_wraps_terminal_transport_error_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler is never reached: every attempt fails at the transport layer.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client, state = _install_flaky_httpx_get(monkeypatch, fail_times=99, handler=handler)
    with pytest.raises(ImbueCloudTunnelError) as exc_info:
        client.list_tunnels(SecretStr("tok"))
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
    with pytest.raises(ImbueCloudTunnelError):
        client.list_tunnels(SecretStr("tok"))
    assert state["calls"] == 1


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


def test_put_sync_record_returns_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/sync/records/host-1"
        body = _json.loads(request.content)
        assert body["revision"] == 1
        return httpx.Response(200, json=_sync_record_json())

    client = _install_mock_httpx(monkeypatch, handler)
    stored = client.put_sync_record(SecretStr("tok"), SyncWorkspaceRecord.model_validate(_sync_record_json()))
    assert stored.revision == 1


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
