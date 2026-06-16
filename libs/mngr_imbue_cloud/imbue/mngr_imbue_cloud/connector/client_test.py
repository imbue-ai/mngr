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
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketExistsError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketLimitError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotFoundError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError


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


def test_create_bucket_key_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/buckets/data/keys"
        assert _json.loads(request.content) == {"access": "read", "alias": "ro"}
        return httpx.Response(
            200,
            json={
                "access_key_id": "akid2",
                "secret_access_key": "s2",
                "s3_endpoint": "https://acct.r2.cloudflarestorage.com",
                "bucket_name": "u--data",
                "access": "read",
            },
        )

    client = _install_mock_httpx(monkeypatch, handler)
    material = client.create_bucket_key(SecretStr("tok"), "data", "ro", "read")
    assert material.access == "read"
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


def test_destroy_bucket_key_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bucket-keys/akid1"
        return httpx.Response(200, json={"status": "deleted"})

    client = _install_mock_httpx(monkeypatch, handler)
    client.destroy_bucket_key(SecretStr("tok"), "akid1")


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
