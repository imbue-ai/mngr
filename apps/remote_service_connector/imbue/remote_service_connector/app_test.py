import base64
import contextlib
import hashlib
import json
import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg2
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.app import AdminAuth
from imbue.remote_service_connector.app import AuthPolicy
from imbue.remote_service_connector.app import CloudflareApiError
from imbue.remote_service_connector.app import ForwardingCtx
from imbue.remote_service_connector.app import HttpCloudflareOps
from imbue.remote_service_connector.app import InvalidR2BucketNameError
from imbue.remote_service_connector.app import InvalidTunnelComponentError
from imbue.remote_service_connector.app import PostgresSyncStore
from imbue.remote_service_connector.app import R2BucketOwnershipError
from imbue.remote_service_connector.app import R2StorageResultTruncatedError
from imbue.remote_service_connector.app import ServiceNotFoundError
from imbue.remote_service_connector.app import SyncActiveAgentConflictError
from imbue.remote_service_connector.app import SyncRevisionConflictError
from imbue.remote_service_connector.app import SyncStoreConsistencyError
from imbue.remote_service_connector.app import TunnelComponentTooLongError
from imbue.remote_service_connector.app import TunnelNotFoundError
from imbue.remote_service_connector.app import TunnelOwnershipError
from imbue.remote_service_connector.app import _MAX_ENCRYPTED_SECRETS_BYTES
from imbue.remote_service_connector.app import _MAX_KEY_BUNDLE_FIELD_BYTES
from imbue.remote_service_connector.app import _authenticate_supertokens
from imbue.remote_service_connector.app import _default_email_getter
from imbue.remote_service_connector.app import cf_check
from imbue.remote_service_connector.app import cf_list_all_pages
from imbue.remote_service_connector.app import clear_paid_status_cache
from imbue.remote_service_connector.app import derive_s3_secret_access_key
from imbue.remote_service_connector.app import extract_service_name
from imbue.remote_service_connector.app import extract_username_from_tunnel_name
from imbue.remote_service_connector.app import get_sync_store
from imbue.remote_service_connector.app import is_email_paid
from imbue.remote_service_connector.app import is_email_paid_in_db
from imbue.remote_service_connector.app import make_bucket_name
from imbue.remote_service_connector.app import make_hostname
from imbue.remote_service_connector.app import make_tunnel_name
from imbue.remote_service_connector.app import require_ally_eligible
from imbue.remote_service_connector.app import slugify_r2_name
from imbue.remote_service_connector.app import verify_bucket_ownership
from imbue.remote_service_connector.app import web_app
from imbue.remote_service_connector.testing import ALLY_PLAN_VALUES
from imbue.remote_service_connector.testing import EXPLORER_PLAN_VALUES
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import FakeLiteLLMBackend
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryEntitlementsStore
from imbue.remote_service_connector.testing import InMemoryGrantStore
from imbue.remote_service_connector.testing import InMemoryKeyStore
from imbue.remote_service_connector.testing import InMemorySyncStore
from imbue.remote_service_connector.testing import make_fake_entitlements_store
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx
from imbue.remote_service_connector.testing import make_fake_grant_store
from imbue.remote_service_connector.testing import make_fake_key_store
from imbue.remote_service_connector.testing import make_fake_litellm_backend
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.testing import make_fake_sync_store
from imbue.remote_service_connector.testing import make_fake_tunnel_token
from imbue.remote_service_connector.testing import noop_enforcement_lock

_ADMIN_STUB_TOKEN = "admin-stub-jwt"
_ADMIN_STUB_USERNAME = "testuser"
_ADMIN_STUB_EMAIL = "testuser@example.com"
_ADMIN_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"
_PAID_ADMIN_KEY_TEST_VALUE = "paid-admin-key-secret-9f3a2b"


def _admin_headers() -> dict[str, str]:
    """Return a Bearer header for a fake SuperTokens admin session.

    Paired with ``_make_test_client`` which stubs ``_authenticate_supertokens``
    to recognise ``_ADMIN_STUB_TOKEN`` and return a canned ``AdminAuth``.
    """
    return {"Authorization": f"Bearer {_ADMIN_STUB_TOKEN}"}


def _agent_headers(tunnel_id: str) -> dict[str, str]:
    token = make_fake_tunnel_token(tunnel_id)
    return {"Authorization": f"Bearer {token}"}


def _make_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemoryEntitlementsStore, FakeLiteLLMBackend]:
    """Create a TestClient with the FastAPI app plus every quota-relevant fake.

    Sets up the SuperTokens Bearer auth path so tests calling admin endpoints
    can authenticate with ``_admin_headers()`` without needing a real JWT.
    Installs an in-memory paid-list backend seeded with the stub admin email,
    an entitlements store pre-seeded with the two launch plans (with the stub
    user's SuperTokens ``time_joined`` faked to 0, i.e. pre-cutoff, so the
    stub's lazy plan resolves to ally by default), and a fake LiteLLM admin
    API. The paid-status cache is disabled
    (``MINDS_PAID_LIST_CACHE_TTL_SECONDS=0``) so the module-level cache never
    bleeds between tests.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    # ``/keys/create`` embeds the proxy URL in its response (the LiteLLM calls
    # themselves go through the installed fake).
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://fake-litellm.example.com")
    fake_ctx = make_fake_forwarding_ctx()

    def _stub_supertokens(token: str) -> AdminAuth:
        if token != _ADMIN_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return AdminAuth(username=_ADMIN_STUB_USERNAME, email=_ADMIN_STUB_EMAIL)

    entitlements_store = make_fake_entitlements_store()
    litellm = make_fake_litellm_backend()
    # Single-loop patching (matches the Fake*Backend.install_on_app_module
    # pattern) so the monkeypatch ratchet only counts one occurrence.
    quota_fakes: dict[str, object] = {
        "get_ctx": lambda: fake_ctx,
        "_authenticate_supertokens": _stub_supertokens,
        "get_entitlements_store": lambda: entitlements_store,
        "_get_user_id_from_access_token": lambda token: _ADMIN_STUB_USER_ID,
        "_get_user_time_joined_ms": lambda user_id, user_getter=None: 0,
        "_litellm_request": litellm.request,
    }
    for name, fake_impl in quota_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app), entitlements_store, litellm


def _make_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with the standard fakes (see ``_make_quota_test_client``)."""
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    return client


_PLAN_VALUES_BY_NAME = {"explorer": EXPLORER_PLAN_VALUES, "ally": ALLY_PLAN_VALUES}


def _seed_entitlements_row(
    entitlements_store: InMemoryEntitlementsStore,
    plan_name: str = "explorer",
    user_id: str = _ADMIN_STUB_USER_ID,
    username_prefix: str = _ADMIN_STUB_USERNAME,
    **overrides: float,
) -> None:
    """Insert an entitlements row copied from the named launch plan, with per-test quota overrides."""
    entitlements_store.insert_entitlements_if_absent(
        {
            "user_id": user_id,
            "username_prefix": username_prefix,
            "plan_name": plan_name,
            **{**_PLAN_VALUES_BY_NAME[plan_name], **overrides},
        }
    )


def _email_policy(email: str) -> AuthPolicy:
    """Build the allow-only-this-email AuthPolicy used across the tunnel/service tests."""
    return AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": email}}]}])


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name("alice", "agent1") == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    assert make_tunnel_name("alice", "agent-abc123") == "alice--abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_username() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Username"):
        make_tunnel_name("alice--bob", "agent1")


def test_make_tunnel_name_truncates_agent_id() -> None:
    result = make_tunnel_name("alice", "agent--1")
    assert result == "alice---1"


def test_make_hostname_format() -> None:
    assert make_hostname("web", "agent1", "alice", "example.com") == "web--agent1--alice.example.com"


def test_extract_service_name_from_hostname() -> None:
    assert extract_service_name("web--agent1--alice.example.com", "agent1", "alice", "example.com") == "web"


def test_extract_service_name_returns_none_for_non_matching() -> None:
    assert extract_service_name("other.example.com", "agent1", "alice", "example.com") is None


def test_extract_username_from_tunnel_name() -> None:
    assert extract_username_from_tunnel_name("alice--agent1") == "alice"


def test_cf_check_raises_on_error() -> None:
    response = httpx.Response(400, json={"success": False, "errors": [{"message": "bad"}]})
    with pytest.raises(CloudflareApiError) as exc_info:
        cf_check(response)
    assert exc_info.value.status_code == 400


def test_cf_check_returns_data_on_success() -> None:
    response = httpx.Response(200, json={"success": True, "result": {"id": "123"}})
    data = cf_check(response)
    assert data["result"]["id"] == "123"


def test_cf_list_all_pages_paginates() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        page = int(dict(request.url.params).get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"id": "1"}, {"id": "2"}],
                    "result_info": {"total_count": 3, "page": 1, "per_page": 2, "count": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"id": "3"}],
                "result_info": {"total_count": 3, "page": 2, "per_page": 2, "count": 1},
            },
        )

    client = httpx.Client(base_url="https://test.example.com", transport=httpx.MockTransport(handler))
    results = cf_list_all_pages(client, "/test", {})
    assert len(results) == 3
    assert call_count == 2


def test_create_tunnel() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    assert info.tunnel_name == "alice--agent1"
    assert info.token == "token-for-tunnel-1"
    assert info.services == []


def test_create_tunnel_with_default_auth() -> None:
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    info = ctx.create_tunnel("alice", "agent1", default_auth_policy=policy)
    assert info.tunnel_name == "alice--agent1"
    stored = ctx.get_tunnel_auth("alice--agent1")
    assert stored is not None
    assert len(stored.rules) == 1


def test_create_tunnel_reuses_existing() -> None:
    ctx = make_fake_forwarding_ctx()
    info1 = ctx.create_tunnel("alice", "agent1")
    info2 = ctx.create_tunnel("alice", "agent1")
    assert info1.tunnel_id == info2.tunnel_id


def test_list_tunnels_filters_by_user() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.create_tunnel("alice", "agent2")
    ctx.create_tunnel("bob", "agent3")
    tunnels = ctx.list_tunnels("alice")
    assert len(tunnels) == 2


def test_get_tunnel_for_agent_returns_none_when_absent() -> None:
    ctx = make_fake_forwarding_ctx()
    assert ctx.get_tunnel_for_agent("alice", "agent1") is None


def test_get_tunnel_for_agent_returns_tunnel_with_services() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    tunnel = ctx.get_tunnel_for_agent("alice", "agent1")
    assert tunnel is not None
    assert tunnel.tunnel_name == "alice--agent1"
    assert [s.service_name for s in tunnel.services] == ["web"]


class _CallCountingCloudflareOps(FakeCloudflareOps):
    """FakeCloudflareOps that counts the O(n)-prone tunnel calls.

    Used to assert the ``get_tunnel_for_agent`` fast path never enumerates the
    account (``list_tunnels``) and fetches only the matched tunnel's config.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_tunnels_calls = 0
        self.get_tunnel_config_calls = 0

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        self.list_tunnels_calls += 1
        return super().list_tunnels(include_prefix=include_prefix)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        self.get_tunnel_config_calls += 1
        return super().get_tunnel_config(tunnel_id)


def test_get_tunnel_for_agent_targets_by_name_not_enumeration() -> None:
    """The O(1) lookup must resolve the exact tunnel without enumerating the
    account (``list_tunnels``) or fetching every tunnel's config.

    Creates many tunnels for the user, then counts the expensive calls: the
    lookup must hit ``get_tunnel_config`` exactly once (for the matched
    tunnel) and never call ``list_tunnels``.
    """
    ops = _CallCountingCloudflareOps()
    ctx = ForwardingCtx(ops=ops, domain="example.com")
    for i in range(10):
        ctx.create_tunnel("alice", f"agent{i}")
    ops.get_tunnel_config_calls = 0
    ops.list_tunnels_calls = 0
    tunnel = ctx.get_tunnel_for_agent("alice", "agent7")
    assert tunnel is not None
    assert tunnel.tunnel_name == "alice--agent7"
    assert ops.get_tunnel_config_calls == 1
    assert ops.list_tunnels_calls == 0


def test_delete_tunnel_cascades() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.delete_tunnel("alice--agent1", "alice")
    assert len(ctx.fake.tunnels) == 0
    assert len(ctx.fake.dns_records) == 0
    assert ctx.fake.kv_get("alice--agent1") is None


def test_delete_tunnel_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(TunnelOwnershipError):
        ctx.delete_tunnel("alice--agent1", "bob")


def test_add_service_creates_dns_and_ingress() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    info = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert info.hostname == "web--agent1--alice.example.com"
    assert len(ctx.fake.dns_records) == 1


def test_add_service_applies_default_access_policy() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert len(ctx.fake.access_policies.get(app_id, [])) == 1


def test_add_service_passes_allowed_idps_to_access_app() -> None:
    """When ForwardingCtx has allowed_idps configured, they are passed to created Access Applications."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123"]


def test_add_service_no_allowed_idps_when_not_configured() -> None:
    """When allowed_idps is None, it is not included in the Access Application."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert "allowed_idps" not in ctx.fake.access_apps[app_id]


def test_set_service_auth_passes_allowed_idps() -> None:
    """set_service_auth creates Access Applications with allowed_idps when configured."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123", "otp-idp-uuid-456"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123", "otp-idp-uuid-456"]


def test_add_service_is_idempotent() -> None:
    """Calling ``add_service`` twice for the same hostname should succeed without
    creating a duplicate CNAME or duplicate ingress rule.

    Real Cloudflare returns error 81053 ("DNS record already exists") on the
    second ``create_cname`` call -- ``FakeCloudflareOps`` mirrors that. Before
    this fix, the minds "Update sharing" flow re-ran ``add_service`` on every
    submit and surfaced the connector's 400/81053 error to the user.
    """
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp"])
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:9090")
    assert len(ctx.fake.dns_records) == 1
    services = ctx.list_services("alice--agent1", "alice")
    assert len(services) == 1
    assert services[0].service_url == "http://localhost:9090"


def test_add_service_preserves_customized_service_auth_on_re_add() -> None:
    """A second ``add_service`` after the user has set a custom service-level
    auth policy must not reset that policy back to the tunnel default."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    default_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "owner@x.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", default_policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")

    custom_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "guest@y.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", custom_policy)

    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert result.rules == custom_policy.rules


def test_add_service_rejects_cname_pointing_elsewhere() -> None:
    """If a CNAME for the hostname exists but points at a different tunnel,
    ``add_service`` must refuse rather than silently leak traffic."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    hostname = make_hostname("web", "agent1", "alice", "example.com")
    ctx.fake.dns_records.append(
        {"id": "stray", "name": hostname, "content": "different-tunnel.cfargotunnel.com", "type": "CNAME"}
    )
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")


def test_remove_service_deletes_access_app() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    ctx.remove_service("alice--agent1", "alice", "web")
    assert len(ctx.fake.access_apps) == 0


def test_remove_service_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(ServiceNotFoundError):
        ctx.remove_service("alice--agent1", "alice", "nonexistent")


def test_tunnel_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    assert ctx.get_tunnel_auth("alice--agent1") is None
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    result = ctx.get_tunnel_auth("alice--agent1")
    assert result is not None
    assert result.rules == policy.rules


def test_service_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert len(result.rules) == 1


def test_resolve_tunnel_name_by_id() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    name = ctx.resolve_tunnel_name_by_id(info.tunnel_id)
    assert name == "alice--agent1"


def test_resolve_tunnel_name_by_id_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    with pytest.raises(TunnelNotFoundError):
        ctx.resolve_tunnel_name_by_id("nonexistent")


def test_route_create_tunnel_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["tunnel_name"] == "testuser--agent1"
    assert data["token"] is not None


def test_route_create_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_list_tunnels_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_get_tunnel_for_agent_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels/by-agent/agent1", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == "testuser--agent1"


def test_route_get_tunnel_for_agent_returns_null_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # 200 + null (not 404) so a client can tell "no tunnel" apart from
    # "this connector predates the endpoint" (an unknown-route 404).
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/by-agent/agent1", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() is None


def test_route_get_tunnel_for_agent_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels/by-agent/agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_add_service_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["service_name"] == "web"


def test_route_add_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 200


def test_route_add_service_agent_wrong_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post("/tunnels", json={"agent_id": "agent2"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent2/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_remove_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.delete("/tunnels/testuser--agent1/services/web", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200


def test_route_delete_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_set_tunnel_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200


def test_route_get_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()["rules"]) == 1


def test_route_set_tunnel_auth_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": []},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_no_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels")
    assert resp.status_code == 401


def test_route_rejects_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """After removing USER_CREDENTIALS, Basic Auth is no longer a supported scheme."""
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert resp.status_code == 401


def test_route_malformed_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/foo--bar/services", headers={"Authorization": "Bearer not-valid-base64!!!"})
    assert resp.status_code == 401


def test_route_create_tunnel_too_long_username_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a tunnel whose authenticated username is too long returns 400, not 500."""
    long_name = "a_very_long_username_exceeds_max"
    client = _make_test_client(monkeypatch)
    # Override the stub to return an AdminAuth with an overly-long username,
    # simulating a SuperTokens session whose user_id_prefix is longer than the
    # tunnel-naming limit.
    monkeypatch.setattr(
        app_mod,
        "_authenticate_supertokens",
        lambda _token: AdminAuth(username=long_name),
    )
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 400


def test_tunnel_component_too_long_error_message() -> None:
    with pytest.raises(TunnelComponentTooLongError) as exc_info:
        raise TunnelComponentTooLongError("Username", "toolong", 5)
    assert "Username" in str(exc_info.value)
    assert "toolong" in str(exc_info.value)
    assert "5" in str(exc_info.value)


# -- _authenticate_supertokens tests --


class _FakeSession:
    """Minimal mock for supertokens SessionContainer."""

    def __init__(self, user_id: str, email_verified: bool = True) -> None:
        self._user_id = user_id
        self._email_verified = email_verified

    def get_user_id(self) -> str:
        return self._user_id

    def get_access_token_payload(self) -> dict[str, object]:
        return {"st-ev": {"v": self._email_verified, "t": 0}}


def test_authenticate_supertokens_returns_admin_auth_with_user_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token returns AdminAuth whose username is the first 16 hex chars of the user ID."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "valid-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
        email_getter=lambda _user_id: "alice@example.com",
    )
    assert isinstance(result, AdminAuth)
    assert result.username == "a1b2c3d4e5f67890"
    assert result.email == "alice@example.com"


def test_authenticate_supertokens_raises_401_when_no_verified_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the live lookup finds no verified email, auth is rejected with 401.

    ``email_getter`` (``_default_email_getter`` in production) returns None both
    when the user has no email and when their only emails are unverified; either
    way the caller has proven no verified identity, so the guard denies access.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "valid-token",
            session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "verified" in exc_info.value.detail


def test_authenticate_supertokens_ignores_stale_unverified_token_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token minted while unverified still authenticates once the core reports the email verified.

    The access token carries a cached ``email_verified=False`` claim, but the
    live ``email_getter`` lookup returns a verified email (e.g. the user was
    just added to the paid list and auto-verified). The guard must trust the
    live result, not the stale token claim, so the request succeeds without the
    user having to refresh their token first.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "stale-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=False),
        email_getter=lambda _user_id: "alice@example.com",
    )
    assert isinstance(result, AdminAuth)
    assert result.email == "alice@example.com"


def test_authenticate_supertokens_raises_401_when_connection_uri_not_set() -> None:
    """When SUPERTOKENS_CONNECTION_URI is absent, raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "any-token",
            session_getter=lambda **kwargs: _FakeSession("ignored"),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "not configured" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_session_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter returns None, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "expired-token",
            session_getter=lambda **kwargs: None,
        )
    assert exc_info.value.status_code == 401


def test_authenticate_supertokens_raises_401_on_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter raises SuperTokensSessionError, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensSessionError("bad session")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


def test_authenticate_supertokens_raises_401_on_general_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK is not initialized (GeneralError), raises 401 instead of 500."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensGeneralError("Initialisation not done")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


# -- _default_email_getter tests --


class _FakeLoginMethod:
    """Stand-in for a SuperTokens LoginMethod -- only ``email`` and ``verified`` are used."""

    def __init__(self, email: str | None, verified: bool = True) -> None:
        self.email = email
        self.verified = verified


class _FakeStUser:
    """Stand-in for a SuperTokens User -- only the ``login_methods`` attribute is used."""

    def __init__(self, login_methods: list[_FakeLoginMethod]) -> None:
        self.login_methods = login_methods


def test_default_email_getter_returns_first_verified_non_empty_email() -> None:
    """The first login method with both a non-empty email and ``verified=True`` is returned."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod(""), _FakeLoginMethod("alice@example.com")])
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@example.com"


def test_default_email_getter_skips_unverified_emails() -> None:
    """Unverified login methods are skipped; the first *verified* email is returned.

    A user with both an unverified third-party login (``evil@gmail.com``) and a verified
    emailpassword login (``alice@imbue.com``) must surface ``alice@imbue.com``, since the
    paid-feature gate authorizes by domain ownership and only verified emails prove that.
    """
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("alice@imbue.com", verified=True),
        ]
    )
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@imbue.com"


def test_default_email_getter_returns_none_when_only_unverified_emails() -> None:
    """When every login method is unverified, returns None even if emails are present."""
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("other@gmail.com", verified=False),
        ]
    )
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_no_login_method_has_email() -> None:
    """When no login method has a non-empty email, returns None."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod("")])
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_user_is_none() -> None:
    """When the SDK reports no user for the id, returns None."""
    assert _default_email_getter("user-123", user_getter=lambda _user_id: None) is None


def test_default_email_getter_returns_none_on_general_error() -> None:
    """When the SDK raises a GeneralError (e.g. transient core problem), it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensGeneralError("transient core problem")

    assert _default_email_getter("user-123", user_getter=_raise) is None


def test_default_email_getter_returns_none_on_session_error() -> None:
    """When the SDK raises a SessionError, it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensSessionError("bad session")

    assert _default_email_getter("user-123", user_getter=_raise) is None


# -- Auth route tests --
#
# These are smoke tests that verify the auth routes are wired up and reject
# calls when SuperTokens is not configured. Exercising the success paths
# requires a real SuperTokens core and is covered by release-marked E2E tests.


def test_auth_signup_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signup without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_signin_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signin without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_session_refresh_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/refresh without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "r"})
    assert resp.status_code == 503


def test_auth_session_revoke_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke", headers={"Authorization": "Bearer any-token"})
    assert resp.status_code == 503


def test_auth_session_revoke_requires_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without a Bearer access token returns 401.

    This guards against an anonymous caller terminating arbitrary users'
    sessions just by knowing (or guessing) their user_id.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke")
    assert resp.status_code == 401


def test_auth_verify_email_missing_token_shows_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify-email endpoint renders an HTML failure page when the token is missing."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email")
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_reset_password_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset-password page renders an HTML form embedding the token."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/reset-password", params={"token": "tok-xyz"})
    assert resp.status_code == 200
    assert "tok-xyz" in resp.text
    assert "Reset password" in resp.text


# -- /auth/* happy-path tests (powered by FakeSuperTokensBackend) --


def _install_fake_supertokens(monkeypatch: pytest.MonkeyPatch) -> FakeSuperTokensBackend:
    """Wire the FakeSuperTokensBackend into the app module and return it."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    backend = make_fake_supertokens_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_auth_signup_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup creates an account, issues a session, and sends a verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == 1
    assert "new@example.com" in backend.accounts_by_email


def test_auth_signup_field_error_on_empty_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup returns FIELD_ERROR for empty email or password."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "  ", "password": "x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FIELD_ERROR"


def test_auth_signup_duplicate_email_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing up with an email that already exists returns EMAIL_ALREADY_EXISTS."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    assert len(backend.accounts_by_email) == 1


def test_auth_signup_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signup is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_up", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ERROR",
        "message": "Auth backend unavailable",
        "user": None,
        "tokens": None,
        "needs_email_verification": False,
    }


def _install_paid_pool_backend(monkeypatch: pytest.MonkeyPatch, *paid_emails: str) -> FakePoolBackend:
    """Install a fake pool backend (so ``is_email_paid`` works) seeding the given paid emails."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    pool_backend = make_fake_pool_backend()
    for paid_email in paid_emails:
        pool_backend.add_paid_email(paid_email)
    pool_backend.install_on_app_module(app_mod, monkeypatch)
    return pool_backend


def test_auth_signup_paid_email_is_auto_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paid user's email/password signup is auto-verified: no email sent, account already verified."""
    st_backend = _install_fake_supertokens(monkeypatch)
    _install_paid_pool_backend(monkeypatch, "paid@example.com")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "paid@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    # The paid account skips the verification round trip entirely.
    assert body["needs_email_verification"] is False
    assert st_backend.sent_verification_emails == []
    assert st_backend.accounts_by_email["paid@example.com"].is_verified is True


def test_auth_signup_unpaid_email_still_requires_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-paid signup keeps the verify-by-email flow (control for the paid case)."""
    st_backend = _install_fake_supertokens(monkeypatch)
    _install_paid_pool_backend(monkeypatch, "someone-else@example.com")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "free@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_email_verification"] is True
    assert len(st_backend.sent_verification_emails) == 1
    assert st_backend.accounts_by_email["free@example.com"].is_verified is False


def test_auth_signin_happy_path_with_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin against a verified account returns OK and skips resending verification."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    initial_verify_count = len(backend.sent_verification_emails)
    account = backend.accounts_by_email["a@b.com"]
    backend.mark_email_verified(account.user_id)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is False
    assert len(backend.sent_verification_emails) == initial_verify_count


def test_auth_signin_wrong_password_returns_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin with an incorrect password returns WRONG_CREDENTIALS without issuing a session."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "WRONG_CREDENTIALS"
    assert body["tokens"] is None


def test_auth_signin_unverified_email_triggers_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in to an unverified account sends another verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv@example.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/signin", json={"email": "unv@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_signin_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signin is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_in", SuperTokensSessionError("session store down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_refresh_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh rotates tokens and invalidates the old refresh token."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "r@e.com", "password": "password123"}).json()
    initial_refresh = signup["tokens"]["refresh_token"]
    resp = client.post("/auth/session/refresh", json={"refresh_token": initial_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["tokens"]["refresh_token"] != initial_refresh
    assert initial_refresh not in backend.sessions_by_refresh_token


def test_auth_session_refresh_rejects_unknown_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh returns status=ERROR for an unknown refresh token."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_revoke_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/revoke tears down every session for the authenticated user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "rev@e.com", "password": "password123"}).json()
    access = signup["tokens"]["access_token"]
    assert len(backend.sessions_by_access_token) == 1
    resp = client.post(
        "/auth/session/revoke",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert resp.json()["revoked_count"] == 1
    assert len(backend.sessions_by_access_token) == 0


def test_auth_send_verification_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/send-verification resends a verification email for a known user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "v@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    before = len(backend.sent_verification_emails)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": user_id, "email": "v@e.com"},
    )
    assert resp.status_code == 200
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_send_verification_email_unknown_user_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending verification email for a user that doesn't exist returns 404."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": "does-not-exist", "email": "a@b.com"},
    )
    assert resp.status_code == 404


def test_auth_is_email_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified reflects the underlying account state."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "iv@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}
    backend.mark_email_verified(user_id)
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.json() == {"verified": True}


def test_auth_is_email_verified_unknown_user_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified returns verified=False for a user that doesn't exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/email/is-verified", json={"user_id": "nope", "email": "a@b.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}


def test_auth_verify_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify-email page consumes a valid token and marks the account verified."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "ve@e.com", "password": "password123"})
    token = next(iter(backend.verification_tokens.keys()))
    resp = client.get("/auth/verify-email", params={"token": token})
    assert resp.status_code == 200
    assert "Email verified" in resp.text
    user_id = backend.accounts_by_email["ve@e.com"].user_id
    assert backend.accounts_by_id[user_id].is_verified is True


def test_auth_verify_email_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting an invalid verification token renders the failure page."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email", params={"token": "bogus"})
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_forgot_password_sends_reset_email_for_known_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/forgot enqueues a reset email when the account exists."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "fp@e.com", "password": "password123"})
    resp = client.post("/auth/password/forgot", json={"email": "fp@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert len(backend.sent_reset_emails) == 1


def test_auth_forgot_password_unknown_email_still_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """For unknown emails the endpoint returns the same success shape (anti-enumeration)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/forgot", json={"email": "nobody@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.sent_reset_emails == []


def test_auth_reset_password_consumes_token_and_updates_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid reset token updates the account password; it cannot be reused."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "rp@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["rp@e.com"].user_id
    token = backend.issue_reset_token(user_id)
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "newpass456"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.accounts_by_id[user_id].password == "newpass456"
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "again789"})
    assert resp.json()["status"] == "INVALID_TOKEN"


def test_auth_reset_password_rejects_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/reset returns 400 when the token or password is missing."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/reset", json={"token": "", "new_password": ""})
    assert resp.status_code == 400


def test_auth_oauth_authorize_returns_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize asks the provider for a redirect URL."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oa@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["url"].startswith("https://google.example.com/auth")


def test_auth_oauth_authorize_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "unknown", "callback_url": "http://127.0.0.1/cb"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_oauth_callback_creates_user_and_returns_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback links the provider user, creates an account, and returns tokens."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider(
        "google",
        email="cb@e.com",
        third_party_user_id="tp-1",
        display_name="Callback User",
    )
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1:9999/cb",
            "query_params": {"code": "abc", "state": "xyz"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "cb@e.com"
    assert body["user"]["display_name"] == "Callback User"
    assert body["tokens"]["access_token"].startswith("at-")
    assert "cb@e.com" in backend.accounts_by_email


def test_auth_oauth_callback_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "missing",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_get_user_returns_provider_email_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports 'email' for password-registered accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "gu@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["gu@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "email"
    assert body["email"] == "gu@e.com"


def test_auth_get_user_reports_third_party_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports the OAuth provider ID for OAuth accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oauth-user@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {"code": "a"},
        },
    )
    user_id = backend.accounts_by_email["oauth-user@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "google"


def test_auth_get_user_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} returns 404 when the user does not exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/users/does-not-exist")
    assert resp.status_code == 404


# -- HttpCloudflareOps tests (via httpx.MockTransport) --
#
# HttpCloudflareOps is the production implementation backed by real Cloudflare
# HTTP calls. These tests wire it up with httpx.MockTransport so every cf_*
# helper and its HttpCloudflareOps wrapper runs without touching the network.


def _cf_result(result: object, *, total_count: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {"success": True, "result": result}
    if total_count is not None and isinstance(result, list):
        body["result_info"] = {
            "total_count": total_count,
            "page": 1,
            "per_page": len(result) or 1,
            "count": len(result),
        }
    return body


def _build_http_ops_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpCloudflareOps:
    """Construct an HttpCloudflareOps whose client is wired to a MockTransport.

    Closes the real httpx.Client that HttpCloudflareOps opens during __init__
    before reassigning ``ops.client`` to the mock-backed client, so tests
    don't leak a connection pool per invocation.
    """
    ops = HttpCloudflareOps(api_token="token", account_id="acc", zone_id="zone")
    ops.client.close()
    ops.client = httpx.Client(base_url="https://api.cloudflare.com/client/v4", transport=httpx.MockTransport(handler))
    return ops


def _build_http_ops_with_routes(
    routes: dict[tuple[str, str], httpx.Response],
) -> HttpCloudflareOps:
    """Construct an HttpCloudflareOps whose client is wired to a MockTransport.

    Each key in ``routes`` is ``(method, path_prefix)``; the first matching
    route returns its response. Requests that don't match any route produce a
    clear AssertionError instead of a silent 404 so new uncovered code paths
    fail loudly in test output.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for (method, path), response in routes.items():
            if request.method == method and request.url.path.startswith(path):
                return response
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return _build_http_ops_with_handler(handler)


def test_http_ops_tunnel_roundtrip() -> None:
    """create_tunnel, list_tunnels, get_tunnel_by_name/id, get_tunnel_token, delete_tunnel."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1/token"): httpx.Response(
            200, json=_cf_result("tunnel-token-value")
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result([{"id": "t1", "name": "alice--a1"}], total_count=1)
        ),
        ("DELETE", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    tunnel = ops.create_tunnel("alice--a1")
    assert tunnel["id"] == "t1"
    assert ops.get_tunnel_token("t1") == "tunnel-token-value"
    assert ops.get_tunnel_by_id("t1") == {"id": "t1", "name": "alice--a1"}
    by_name = ops.get_tunnel_by_name("alice--a1")
    assert by_name is not None and by_name["id"] == "t1"
    tunnels = ops.list_tunnels(include_prefix="alice")
    assert len(tunnels) == 1
    ops.delete_tunnel("t1")


def test_http_ops_get_tunnel_by_id_returns_none_on_404() -> None:
    """cf_get_tunnel_by_id returns None (not raising) when the tunnel is missing."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/missing"): httpx.Response(
            404, json={"success": False, "errors": [{"message": "not found"}]}
        ),
    }
    ops = _build_http_ops_with_routes(routes)
    assert ops.get_tunnel_by_id("missing") is None


def test_http_ops_tunnel_config_roundtrip() -> None:
    """get_tunnel_config and put_tunnel_config both route through cf_check."""
    put_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/configurations" in request.url.path:
            return httpx.Response(200, json=_cf_result({"config": {"ingress": []}}))
        if request.method == "PUT" and "/configurations" in request.url.path:
            put_calls.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    config = ops.get_tunnel_config("t1")
    assert "config" in config
    ops.put_tunnel_config("t1", {"config": {"ingress": [{"service": "http_status:404"}]}})
    assert len(put_calls) == 1


def test_http_ops_dns_record_roundtrip() -> None:
    """create_cname, list_dns_records (with filter), delete_dns_record."""
    created: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/dns_records"):
            created.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "r1", "name": "x.example.com"}))
        if request.method == "GET" and request.url.path.endswith("/dns_records"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "r1", "name": "x.example.com"}], total_count=1),
            )
        if request.method == "DELETE" and "/dns_records/r1" in request.url.path:
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    record = ops.create_cname("x.example.com", "target.example.com")
    assert record["id"] == "r1"
    assert created[0]["type"] == "CNAME"
    assert created[0]["proxied"] is True
    records = ops.list_dns_records(name="x.example.com")
    assert len(records) == 1
    ops.delete_dns_record("r1")


def test_http_ops_access_app_and_policies_roundtrip() -> None:
    """Full Access Application + policy lifecycle flows through the real wrappers."""
    policies: list[dict[str, object]] = []
    created_apps: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/access/apps"):
            created_apps.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "app1", "domain": "x.example.com"}))
        if request.method == "GET" and path.endswith("/access/apps"):
            return httpx.Response(200, json=_cf_result([{"id": "app1", "domain": "x.example.com"}]))
        if request.method == "DELETE" and "/access/apps/app1/policies/p1" in path:
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "DELETE" and path.endswith("/access/apps/app1"):
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "GET" and "/access/apps/app1/policies" in path:
            return httpx.Response(200, json=_cf_result(list(policies)))
        if request.method == "POST" and "/access/apps/app1/policies" in path:
            body = json.loads(request.content.decode())
            policy_record = {**body, "id": "p1"}
            policies.append(policy_record)
            return httpx.Response(200, json=_cf_result(policy_record))
        if request.method == "PUT" and "/access/apps/app1/policies/p1" in path:
            body = json.loads(request.content.decode())
            policies[0] = {**body, "id": "p1"}
            return httpx.Response(200, json=_cf_result(policies[0]))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.create_access_app("x.example.com", "My App", allowed_idps=["idp-1"])
    assert created_apps[0]["allowed_idps"] == ["idp-1"]
    by_domain = ops.get_access_app_by_domain("x.example.com")
    assert by_domain is not None and by_domain["id"] == "app1"
    created_policy = ops.create_access_policy("app1", {"name": "allow", "decision": "allow"})
    assert created_policy["id"] == "p1"
    listed = ops.list_access_policies("app1")
    assert len(listed) == 1
    ops.update_access_policy("app1", "p1", {"name": "allow-updated", "decision": "allow"})
    assert ops.list_access_policies("app1")[0]["name"] == "allow-updated"
    ops.delete_access_policy("app1", "p1")
    ops.delete_access_app("app1")


def test_http_ops_kv_namespace_create_when_missing() -> None:
    """kv_get/kv_put/kv_delete + namespace creation path."""
    stored: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result([]))
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result({"id": "ns1", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns1/values/" in path:
            key = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                if key not in stored:
                    return httpx.Response(404)
                return httpx.Response(200, text=stored[key])
            if request.method == "PUT":
                stored[key] = request.content.decode()
                return httpx.Response(200, json=_cf_result(None))
            if request.method == "DELETE":
                stored.pop(key, None)
                return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    assert ops.kv_get("missing") is None
    ops.kv_put("alice--a1", '{"default": "allow"}')
    assert ops.kv_get("alice--a1") == '{"default": "allow"}'
    ops.kv_delete("alice--a1")
    assert ops.kv_get("alice--a1") is None


def test_http_ops_kv_namespace_reuses_existing() -> None:
    """cf_kv_ensure_namespace returns the existing namespace's id without creating a new one."""
    create_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "ns-existing", "title": "cloudflare-forwarding-defaults"}]),
            )
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            create_calls += 1
            return httpx.Response(200, json=_cf_result({"id": "ns-new", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns-existing/values/" in path and request.method == "PUT":
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.kv_put("k", "v")
    assert create_calls == 0


def test_http_ops_service_token_roundtrip() -> None:
    """create_service_token, list_service_tokens, delete_service_token."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result({"id": "svc1", "client_id": "cid", "client_secret": "sec"})
        ),
        ("GET", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result([{"id": "svc1"}])
        ),
        ("DELETE", "/client/v4/accounts/acc/access/service_tokens/svc1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    token = ops.create_service_token("name")
    assert token["id"] == "svc1"
    assert len(ops.list_service_tokens()) == 1
    ops.delete_service_token("svc1")


# -- Uncovered route and ctx-method tests --


def test_route_get_service_auth_reports_owner_email_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service added with no explicit policy carries the owner-email default Access policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services/web/auth", headers=_admin_headers())
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["include"] == [{"email": {"email": _ADMIN_STUB_EMAIL}}]


def test_route_set_service_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /tunnels/.../services/.../auth admin path persists the policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "updated"}


def test_route_get_tunnel_auth_reports_owner_email_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tunnel created with no explicit policy gets the owner-email default written to KV."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["include"] == [{"email": {"email": _ADMIN_STUB_EMAIL}}]


def test_route_create_and_list_service_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST/GET /tunnels/.../service-tokens round-trip through ForwardingCtx."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-token"
    assert body["client_secret"] is not None
    resp = client.get("/tunnels/testuser--agent1/service-tokens", headers=_admin_headers())
    assert resp.status_code == 200
    listed = resp.json()
    # FakeCloudflareOps.list_service_tokens returns an empty list by design (it
    # doesn't persist created tokens), so the listing is empty -- the test
    # still covers the endpoint + ForwardingCtx.list_service_tokens path.
    assert listed == []


def test_route_service_tokens_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent Bearer auth can't create service tokens (admin-only)."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services admin path lists services."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_delete_tunnel_admin_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin can delete a tunnel they own."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_admin_headers())
    assert resp.status_code == 200
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.json() == []


def test_ctx_set_tunnel_auth_is_persisted_in_kv() -> None:
    """set_tunnel_auth writes the JSON policy to the KV namespace keyed by tunnel name."""
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    stored_raw = ctx.fake.kv_get("alice--agent1")
    assert stored_raw is not None
    assert "a@b.com" in stored_raw


def test_ctx_remove_service_scrubs_ingress_rule() -> None:
    """Removing a service drops its hostname from the tunnel config's ingress."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.remove_service("alice--agent1", "alice", "web")
    config = ctx.fake.tunnel_configs[info.tunnel_id]
    hostnames = [r.get("hostname") for r in config["config"]["ingress"] if "hostname" in r]
    assert hostnames == []


def test_ctx_create_service_token_and_list() -> None:
    """create_service_token persists to the ops layer and returns a ServiceTokenInfo."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    token = ctx.create_service_token("alice--agent1", "alice", "svc-1")
    assert token.name == "svc-1"
    assert token.client_secret is not None
    # FakeCloudflareOps.list_service_tokens returns []; list_service_tokens should
    # reflect that rather than pulling from an internal cache.
    assert ctx.list_service_tokens() == []


# -- Host pool endpoint tests --


def _make_pool_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend, InMemoryEntitlementsStore, FakeLiteLLMBackend]:
    """Create a TestClient with tunnel-auth, pool-backend, and quota fakes installed.

    The returned pool backend is seeded with the stub admin email as paid, so
    the stub's lazily-created entitlements row resolves to the ally plan by
    default; explorer-plan tests flip the entry via ``backend.add_paid_email``
    or write a row into the entitlements store directly.
    """
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "fake-management-key-pem")
    backend = pool_backend if pool_backend is not None else make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, entitlements_store, litellm


def _make_pool_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend]:
    """Pool test client without the quota handles (see ``_make_pool_quota_test_client``)."""
    client, backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch, pool_backend)
    return client, backend


def test_lease_host_returns_available_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns a host when one is available with matching version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
        agent_id="agent-111",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_db_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["vps_address"] == "10.0.0.1"
    assert body["agent_id"] == "agent-111"
    assert body["host_name"] == "my-workspace"
    assert body["attributes"] == {"version": "v0.1.0"}
    # The lease returns both pinned sshd host keys so the client can verify the
    # host strictly instead of trust-on-first-use.
    assert body["outer_host_public_key"]
    assert body["container_host_public_key"]
    # Verify SSH key was injected on both VPS and container, each pinning the
    # corresponding recorded host key (the 6th element of the recorded call).
    assert len(backend.append_key_calls) == 2
    injected_ports = {call[1]: call[5] for call in backend.append_key_calls}
    assert injected_ports[22] == body["outer_host_public_key"]
    assert injected_ports[2222] == body["container_host_public_key"]
    # Verify host was marked as leased and the user-supplied host_name was
    # written to the row.
    assert backend.pool_rows[0].status == "leased"
    assert backend.pool_rows[0].leased_to_user == _ADMIN_STUB_USERNAME
    assert backend.pool_rows[0].host_name == "my-workspace"


def test_lease_host_fails_closed_when_host_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool row with no pinned host keys is not leasable (no trust-on-first-use)."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        outer_host_public_key=None,
        container_host_public_key=None,
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "host-key backfill" in resp.json()["detail"]
    # The row must NOT have been leased, and no SSH key injection was attempted.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_lease_host_returns_503_when_pool_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when no hosts are available."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]


def test_lease_host_returns_503_when_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when available hosts have a different version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.2.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]
    # Verify the host was not leased
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_filters_out_other_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` only leases a host in that datacenter; otherwise 503."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-WEST-OR",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_leases_matching_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` leases a host whose region matches."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-EAST-VA",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"


def test_lease_host_rejects_invalid_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease rejects host_name values that fail the SafeName regex."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "bad.name",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    # The available row stays available since validation rejected the request
    # before the SELECT/UPDATE.
    assert backend.pool_rows[0].status == "available"


def test_rename_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename updates the mutable host_name for the owning user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000051"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000051/rename",
        json={"host_name": "renamed-host"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_name"] == "renamed-host"
    assert backend.pool_rows[0].host_name == "renamed-host"


def test_rename_host_rejects_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename rejects a host_name that fails the SafeName regex (422)."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000052"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    original_name = backend.pool_rows[0].host_name
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000052/rename",
        json={"host_name": "bad.name"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    # The name is unchanged since validation rejected the request before the UPDATE.
    assert backend.pool_rows[0].host_name == original_name


def test_rename_host_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 404 when no such host row exists."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-0000000000ff/rename",
        json={"host_name": "whatever"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


def test_rename_host_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 403 when the host is leased by another user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000053"), version="v0.1.0", leased_to_user="someone-else"
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000053/rename",
        json={"host_name": "renamed-host"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    assert backend.pool_rows[0].host_name != "renamed-host"


def test_rename_host_404_when_not_leased(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 404 when the requester owns the row but it is not leased."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000054"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000054/rename",
        json={"host_name": "renamed-host"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404
    assert backend.pool_rows[0].host_name != "renamed-host"


def test_release_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release destroys the slice's lima VM and drops the row."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    # Row fully cleaned up (deleted) after the slice VM teardown ran.
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1


def test_release_host_idempotent_when_already_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release on a row already in 'removing' re-drives cleanup and returns 200."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000077"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000077/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1


def test_release_host_fails_loudly_when_slice_teardown_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed slice VM teardown makes release return an error -- never a false success.

    Synchronous release contract: a "released" 200 must mean the slice VM is actually
    destroyed. When the teardown fails the endpoint returns 5xx and keeps the row as
    'removing' so the client retries -- never a 200 that silently strands the VM.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.slice_teardown_should_fail = True
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000099"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000099/release", headers=_admin_headers())
    assert resp.status_code == 500
    # The row is NOT deleted; it stays 'removing' so the teardown is retryable.
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"


def test_release_host_returns_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release returns 403 when the caller is not the lease owner."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user="other-user"
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 403
    assert "do not own" in resp.json()["detail"]
    # Verify the host was not released
    assert backend.pool_rows[0].status == "leased"


def test_release_host_unknown_returns_already_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release on a missing row returns 200 already_released (idempotent)."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000999/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_released"


def test_list_hosts_returns_leased_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /hosts returns only hosts leased by the authenticated user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-aaa",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        leased_to_user="other-user",
        agent_id="agent-bbb",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000003"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-ccc",
    )
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 200
    hosts = resp.json()
    assert len(hosts) == 2
    host_ids = {h["host_db_id"] for h in hosts}
    assert host_ids == {"00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000003"}


# -- PAID_ACCOUNT_SUFFIXES gate tests --
# -- Paid-list gate tests (paid_domains / paid_emails tables) --


def _paid_lookup_backend(
    *,
    paid_domains: tuple[str, ...] = (),
    paid_emails: tuple[str, ...] = (),
) -> Callable[[], Any]:
    """Build a connection factory over a fake backend seeded with the given lists."""
    backend = make_fake_pool_backend()
    for domain in paid_domains:
        backend.add_paid_domain(domain)
    for email in paid_emails:
        backend.add_paid_email(email)
    return backend.get_connection


@pytest.mark.parametrize(
    ("email", "paid_domains", "paid_emails", "expected"),
    [
        # Exact domain match (case-insensitive on both sides).
        ("alice@imbue.com", ("imbue.com",), (), True),
        ("ALICE@IMBUE.COM", ("imbue.com",), (), True),
        ("alice@imbue.com", ("IMBUE.COM",), (), True),
        # Subdomains do NOT match a bare-domain entry (exact match only).
        ("alice@eng.imbue.com", ("imbue.com",), (), False),
        ("alice@eng.imbue.com", ("eng.imbue.com",), (), True),
        # Full-email match.
        ("bob@gmail.com", (), ("bob@gmail.com",), True),
        ("eve@gmail.com", (), ("bob@gmail.com",), False),
        # Either list grants access.
        ("carol@imbue.com", ("imbue.com",), ("dave@elsewhere.com",), True),
        # Empty lists deny everyone.
        ("alice@imbue.com", (), (), False),
    ],
)
def test_is_email_paid_in_db_matching(
    email: str,
    paid_domains: tuple[str, ...],
    paid_emails: tuple[str, ...],
    expected: bool,
) -> None:
    factory = _paid_lookup_backend(paid_domains=paid_domains, paid_emails=paid_emails)
    assert is_email_paid_in_db(email, connection_factory=factory) is expected


def test_is_email_paid_in_db_ignores_soft_deleted_rows() -> None:
    backend = make_fake_pool_backend()
    backend.add_paid_email("bob@gmail.com", is_paid=False)
    backend.add_paid_domain("imbue.com", is_paid=False)
    assert is_email_paid_in_db("bob@gmail.com", connection_factory=backend.get_connection) is False
    assert is_email_paid_in_db("alice@imbue.com", connection_factory=backend.get_connection) is False


def test_is_email_paid_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a positive TTL, a second lookup is served from cache (db_lookup not re-invoked)."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    # 30s later: still within the 60s window, so the cached value is reused.
    fake_clock[0] = 1030.0
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    assert call_count == 1
    clear_paid_status_cache()


def test_is_email_paid_refreshes_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    # 61s later: past the window, so the lookup runs again.
    fake_clock[0] = 1061.0
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    assert call_count == 2
    clear_paid_status_cache()


def test_is_email_paid_bypasses_cache_when_ttl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    assert call_count == 2


def test_require_ally_eligible_allows_when_email_is_listed() -> None:
    require_ally_eligible("alice@imbue.com", paid_checker=lambda email: True)


def test_require_ally_eligible_raises_403_when_email_not_listed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible("alice@elsewhere.com", paid_checker=lambda email: False)
    assert exc_info.value.status_code == 403
    assert "partner access" in exc_info.value.detail


def test_require_ally_eligible_raises_403_when_email_is_none() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible(None, paid_checker=lambda email: True)
    assert exc_info.value.status_code == 403
    assert "email unavailable" in exc_info.value.detail


def test_require_ally_eligible_fails_closed_on_db_error() -> None:
    """A database error during the lookup denies eligibility (403), never allows it."""

    def _raise_db_error(email: str) -> bool:
        raise psycopg2.OperationalError("connection refused")

    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible("alice@imbue.com", paid_checker=_raise_db_error)
    assert exc_info.value.status_code == 403
    assert "database error" in exc_info.value.detail


def test_route_lease_host_succeeds_for_unpaid_explorer_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpaid account resolves to the explorer plan and can still lease (quota permitting)."""
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"
    # The lazily-created row is on explorer (unpaid email).
    row = entitlements_store.get_entitlements(_ADMIN_STUB_USER_ID)
    assert row is not None
    assert row["plan_name"] == "explorer"


def test_route_lease_host_returns_quota_403_at_workspace_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease past the account's max_remote_workspaces is refused with structured detail."""
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=1)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_remote_workspaces"
    assert detail["limit"] == 1
    assert detail["current"] == 1
    # No side effects: the available host stays available, no SSH key injection.
    available = [row for row in backend.pool_rows if row.status == "available"]
    assert len(available) == 1
    assert backend.append_key_calls == []


def test_route_release_host_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release only needs ownership -- an account that lost paid status can still release."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert backend.pool_rows == []


def test_route_list_hosts_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_route_create_litellm_key_refused_for_zero_budget_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explorer account (monthly LLM budget 0) cannot mint imbue-cloud keys.

    The refusal happens before any LiteLLM HTTP call and carries the
    structured quota detail plus the subscription guidance.
    """
    client, backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/keys/create", json={}, headers=_admin_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "monthly_llm_spend_usd"
    assert "subscription" in detail["message"]
    assert litellm.calls == []


def test_route_create_litellm_key_upserts_user_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minting a key first pushes the account's monthly budget to LiteLLM as a user budget."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    resp = client.post("/keys/create", json={"key_alias": "my-agent"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["key"].startswith("sk-fake-")
    user = litellm.users_by_id[_ADMIN_STUB_USER_ID]
    assert user["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]
    assert user["budget_duration"] == "1mo"
    assert litellm.generated_keys[0]["user_id"] == _ADMIN_STUB_USER_ID


def test_route_create_litellm_key_fails_when_budget_push_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LiteLLM outage during the budget upsert fails the mint (no key is created)."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    litellm.fail_user_writes = True
    resp = client.post("/keys/create", json={}, headers=_admin_headers())
    assert resp.status_code == 500
    assert litellm.generated_keys == []


def test_route_list_litellm_keys_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing keys needs no quota -- an unpaid (explorer) account gets its (empty) list."""
    client, backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_route_get_litellm_key_enforces_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key info is only served to the key's owner."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    created = client.post("/keys/create", json={}, headers=_admin_headers()).json()
    owned = client.get(f"/keys/{created['key']}", headers=_admin_headers())
    assert owned.status_code == 200
    assert owned.json()["user_id"] == _ADMIN_STUB_USER_ID
    litellm.keys_by_id[created["key"]]["user_id"] = "someone-else"
    foreign = client.get(f"/keys/{created['key']}", headers=_admin_headers())
    assert foreign.status_code == 403


def test_route_update_and_delete_litellm_key_work_without_paid_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget update + delete only require ownership, not any plan gate."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    created = client.post("/keys/create", json={}, headers=_admin_headers()).json()
    resp = client.put(f"/keys/{created['key']}/budget", json={"max_budget": 5.0}, headers=_admin_headers())
    assert resp.status_code == 200
    assert litellm.keys_by_id[created["key"]]["max_budget"] == 5.0
    resp = client.delete(f"/keys/{created['key']}", headers=_admin_headers())
    assert resp.status_code == 200
    assert created["key"] not in litellm.keys_by_id


def test_route_create_tunnel_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare forwarding (`/tunnels/*`) must work even when the user is not paid."""
    client, backend = _make_pool_test_client(monkeypatch)
    # Not-paid: the tunnel route should be unaffected by the paid gate.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == f"{_ADMIN_STUB_USERNAME}--agent1"


def test_route_list_services_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel services routes work for any verified email regardless of paid status."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    create_resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert create_resp.status_code == 200
    list_resp = client.get(f"/tunnels/{_ADMIN_STUB_USERNAME}--agent1/services", headers=_admin_headers())
    assert list_resp.status_code == 200


# -- Paid-list CRUD endpoint tests (admin-key authenticated) --


def _paid_admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_PAID_ADMIN_KEY_TEST_VALUE}"}


def _make_paid_crud_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """Test client with the paid-admin key configured and a fresh paid-list backend."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    return client, backend


def test_paid_crud_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # No Authorization header.
    assert client.get("/paid/domains").status_code == 401
    # Wrong key.
    bad = client.get("/paid/domains", headers={"Authorization": "Bearer wrong-key"})
    assert bad.status_code == 401
    # A SuperTokens admin JWT is NOT accepted on the paid CRUD endpoints.
    assert client.get("/paid/domains", headers=_admin_headers()).status_code == 401


def test_paid_crud_rejects_non_ascii_bearer_token_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII bearer credential is a clean 401, not a 500 (compare over bytes)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # HTTP header values are latin-1; pass raw bytes (both key and value, which
    # matches httpx's ``Mapping[bytes, bytes]`` header type) so the non-ASCII
    # octets reach the handler -- httpx would otherwise reject a non-ASCII str.
    resp = client.get("/paid/domains", headers={b"Authorization": "Bearer wröng-kéy".encode("latin-1")})
    assert resp.status_code == 401


def test_paid_crud_returns_403_when_admin_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.delenv("MINDS_PAID_ADMIN_KEY", raising=False)
    resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["detail"]


def test_paid_admin_key_is_rejected_on_user_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin key must not authenticate user-facing routes (e.g. /hosts)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.get("/hosts", headers=_paid_admin_headers())
    assert resp.status_code == 401


def test_add_and_list_paid_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    add_resp = client.post("/paid/domains/add", json={"value": "Imbue.com"}, headers=_paid_admin_headers())
    assert add_resp.status_code == 200
    assert add_resp.json() == {"status": "added", "domain": "imbue.com"}
    list_resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert [r["domain"] for r in rows] == ["imbue.com"]
    assert rows[0]["is_paid"] is True


def test_remove_paid_domain_is_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    remove_resp = client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert remove_resp.status_code == 200
    # The row is still present (soft delete), but is_paid is now false.
    all_rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert [(r["domain"], r["is_paid"]) for r in all_rows] == [("imbue.com", False)]
    # paid_only filter hides it.
    paid_rows = client.get("/paid/domains?paid_only=true", headers=_paid_admin_headers()).json()
    assert paid_rows == []


def test_re_adding_soft_removed_domain_reactivates_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    original = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    reactivated = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    assert reactivated["is_paid"] is True
    # created_at is preserved across the remove/re-add cycle.
    assert reactivated["created_at"] == original["created_at"]


def test_add_paid_domain_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    first = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    second = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert len(rows) == 1


def test_remove_absent_paid_email_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/remove", json={"value": "nobody@nowhere.com"}, headers=_paid_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed", "email": "nobody@nowhere.com"}


def test_add_paid_email_then_ally_plan_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: adding a paid email via CRUD makes the ally plan selectable."""
    client, backend = _make_paid_crud_test_client(monkeypatch)
    # Start from a clean slate where the stub email is not paid.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    denied = client.post("/account/plan", json={"plan": "ally"}, headers=_admin_headers())
    assert denied.status_code == 403
    assert "partner access" in denied.json()["detail"]
    client.post("/paid/emails/add", json={"value": _ADMIN_STUB_EMAIL}, headers=_paid_admin_headers())
    allowed = client.post("/account/plan", json={"plan": "ally"}, headers=_admin_headers())
    assert allowed.status_code == 200
    assert allowed.json()["plan_name"] == "ally"


def test_add_paid_email_verifies_existing_unverified_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding an email to the paid list verifies a pre-existing (unverified) account for it."""
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    # A user who signed up earlier but never verified their email.
    st_backend.sign_up(tenant_id="public", email="waiting@example.com", password="password123")
    assert st_backend.accounts_by_email["waiting@example.com"].is_verified is False

    resp = client.post("/paid/emails/add", json={"value": "waiting@example.com"}, headers=_paid_admin_headers())

    assert resp.status_code == 200
    assert st_backend.accounts_by_email["waiting@example.com"].is_verified is True


def test_add_paid_email_with_no_existing_account_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a paid email with no matching account still succeeds; there is nothing to verify."""
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)

    resp = client.post("/paid/emails/add", json={"value": "nobody@example.com"}, headers=_paid_admin_headers())

    assert resp.status_code == 200
    assert resp.json() == {"status": "added", "email": "nobody@example.com"}
    assert "nobody@example.com" not in st_backend.accounts_by_email


def test_add_paid_email_succeeds_when_supertokens_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens outage during the auto-verify side effect must not fail the paid-list write.

    No SuperTokens fake is installed, so the real (uninitialized) SDK raises when
    the handler tries to look the account up; that error must be swallowed and
    the paid-list add must still succeed.
    """
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)

    resp = client.post("/paid/emails/add", json={"value": "someone@example.com"}, headers=_paid_admin_headers())

    assert resp.status_code == 200
    assert resp.json() == {"status": "added", "email": "someone@example.com"}


@pytest.mark.parametrize("bad_value", ["", "   ", "has space", "foo@bar.com"])
def test_add_paid_domain_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/domains/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_value", ["", "no-at-sign", "@nodomain", "local@", "a b@c.com"])
def test_add_paid_email_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


# -- R2 bucket endpoint tests --


def _make_bucket_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore, InMemoryEntitlementsStore, InMemoryGrantStore]:
    """Create a TestClient with the R2 fakes installed (Cloudflare ops + key/grant stores + entitlements)."""
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    # Build our own fake ctx so the fake is typed as FakeForwardingCtx (which
    # exposes ``.fake``); re-patching get_ctx overrides the one the quota
    # client installed.
    fake_ctx = make_fake_forwarding_ctx()
    store = make_fake_key_store()
    grant_store = make_fake_grant_store()
    # Single-loop patching (same pattern as the Fake*Backend.install_on_app_module
    # helpers) so the monkeypatch ratchet only counts one occurrence.
    bucket_fakes: dict[str, object] = {
        "get_ctx": lambda: fake_ctx,
        "get_key_store": lambda: store,
        "get_grant_store": lambda: grant_store,
    }
    for name, fake_impl in bucket_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    return client, fake_ctx.fake, store, entitlements_store, grant_store


def _make_bucket_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore]:
    """Bucket test client without the entitlements/grant handles (see ``_make_bucket_quota_test_client``)."""
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    return client, fake, store


# --- Unit tests for naming + helpers ---


def test_make_bucket_name_slugifies() -> None:
    assert make_bucket_name("user", "My Cool Data") == "user--my-cool-data"


def test_make_bucket_name_collapses_separators() -> None:
    assert make_bucket_name("user", "foo__bar--baz") == "user--foo-bar-baz"


def test_make_bucket_name_rejects_invalid() -> None:
    with pytest.raises(InvalidR2BucketNameError):
        make_bucket_name("user", "!!!")


def test_slugify_r2_name_strips_edges() -> None:
    assert slugify_r2_name("  --Foo--  ") == "foo"


def test_verify_bucket_ownership_rejects_foreign_prefix() -> None:
    with pytest.raises(R2BucketOwnershipError):
        verify_bucket_ownership("evil-user--x", "user")


def test_verify_bucket_ownership_accepts_owned() -> None:
    verify_bucket_ownership("user--x", "user")


def test_derive_s3_secret_matches_sha256() -> None:
    assert derive_s3_secret_access_key("hello") == hashlib.sha256(b"hello").hexdigest()


# --- Route tests ---


def test_create_bucket_returns_bucket_and_default_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "my-data"}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"]["bucket_name"] == "testuser--my-data"
    assert body["bucket"]["s3_endpoint"] == "https://test-account.r2.cloudflarestorage.com"
    assert body["key"]["access"] == "readwrite"
    assert body["key"]["bucket_name"] == "testuser--my-data"
    access_key_id = body["key"]["access_key_id"]
    assert access_key_id
    # Secret is the sha256 of the fake token value, returned once.
    assert body["key"]["secret_access_key"] == derive_s3_secret_access_key(f"token-value-{access_key_id}")
    # Bucket actually created in the fake.
    assert "testuser--my-data" in fake.buckets
    # Key metadata recorded; the secret/token value is NOT persisted.
    rows = store.list_keys(_ADMIN_STUB_USER_ID, None)
    assert len(rows) == 1
    assert rows[0]["access_key_id"] == access_key_id
    assert "secret_access_key" not in rows[0]
    assert "value" not in rows[0]
    assert rows[0]["owner_user_id"] == _ADMIN_STUB_USER_ID


def test_create_bucket_with_read_access(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "ro", "access": "read"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["key"]["access"] == "read"


def test_create_bucket_invalid_access_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "x", "access": "write"}, headers=_admin_headers())
    assert resp.status_code == 422


def test_create_bucket_invalid_name_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "!!!"}, headers=_admin_headers())
    assert resp.status_code == 400


def test_create_bucket_duplicate_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "dup"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "dup"}, headers=_admin_headers())
    assert resp.status_code == 409


def test_create_bucket_at_quota_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bucket creation past the account's max_buckets entitlement is refused."""
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_buckets=1)
    assert client.post("/buckets", json={"name": "first"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "one-more"}, headers=_admin_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_buckets"
    assert "testuser--one-more" not in fake.buckets


def test_list_buckets_returns_only_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "a"}, headers=_admin_headers())
    client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    # A bucket owned by someone else, plus a crafted name that merely *contains*
    # the prefix -- the in-code startswith re-check must exclude it.
    fake.buckets["otheruser--secret"] = {"name": "otheruser--secret"}
    fake.buckets["evil-testuser--x"] = {"name": "evil-testuser--x"}
    resp = client.get("/buckets", headers=_admin_headers())
    assert resp.status_code == 200
    names = sorted(b["bucket_name"] for b in resp.json())
    assert names == ["testuser--a", "testuser--b"]


def test_get_bucket_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    resp = client.get("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["bucket_name"] == "testuser--data"


def test_get_bucket_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.get("/buckets/missing", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_bucket_non_empty_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    fake.bucket_objects["testuser--data"].append("obj1")
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 409
    assert "testuser--data" in fake.buckets


def test_destroy_bucket_empty_cascades_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    # A legacy second key (pre-single-key model) must be cascaded too.
    extra = fake.create_bucket_token("testuser--data", "read", "mngr-r2:testuser--data:extra")
    store.add_key(str(extra["id"]), _ADMIN_STUB_USER_ID, "testuser--data", "read", "extra")
    assert len(store.list_keys(_ADMIN_STUB_USER_ID, None)) == 2
    assert len(fake.account_tokens) == 2
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert "testuser--data" not in fake.buckets
    assert store.list_keys(_ADMIN_STUB_USER_ID, None) == []
    assert fake.account_tokens == {}


def test_roll_key_returns_same_access_key_id_with_fresh_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling keeps the Access Key ID (and token policies) while re-deriving the secret."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    original_key = created["key"]
    resp = client.post("/buckets/data/roll-key", headers=_admin_headers())
    assert resp.status_code == 200
    rolled = resp.json()
    assert rolled["access_key_id"] == original_key["access_key_id"]
    assert rolled["secret_access_key"] != original_key["secret_access_key"]
    # Still exactly one recorded key for the bucket.
    assert len(store.list_keys(_ADMIN_STUB_USER_ID, "testuser--data")) == 1


def test_roll_key_reports_enforced_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key downgraded by the storage sweep reports read access through a roll (no bypass)."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    store.set_enforced_access(created["key"]["access_key_id"], "read")
    resp = client.post("/buckets/data/roll-key", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "read"


def test_roll_key_mints_fresh_key_when_none_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling a bucket with no recorded key (e.g. after a revoke) mints one."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    client.delete(f"/bucket-keys/{created['key']['access_key_id']}", headers=_admin_headers())
    assert store.list_keys(_ADMIN_STUB_USER_ID, "testuser--data") == []
    resp = client.post("/buckets/data/roll-key", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "readwrite"
    assert len(store.list_keys(_ADMIN_STUB_USER_ID, "testuser--data")) == 1


def test_roll_key_for_missing_bucket_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets/nope/roll-key", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    create = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    access_key_id = create["key"]["access_key_id"]
    resp = client.delete(f"/bucket-keys/{access_key_id}", headers=_admin_headers())
    assert resp.status_code == 200
    assert access_key_id not in fake.account_tokens
    assert store.get_key(access_key_id) is None


def test_destroy_key_unknown_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.delete("/bucket-keys/does-not-exist", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key_not_owned_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, store = _make_bucket_test_client(monkeypatch)
    store.add_key("akid-other", "some-other-user", "other--bucket", "readwrite", "x")
    resp = client.delete("/bucket-keys/akid-other", headers=_admin_headers())
    assert resp.status_code == 404
    # The other user's row is untouched.
    assert store.get_key("akid-other") is not None


def test_create_bucket_works_for_unpaid_explorer_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old paid gate is gone: an unpaid (explorer) account can create buckets within quota."""
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    # Install a paid-list backend where the stub admin email is NOT paid.
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    backend.install_on_app_module(app_mod, monkeypatch)
    resp = client.post("/buckets", json={"name": "x"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert "testuser--x" in fake.buckets


def test_r2_keys_migration_declares_all_persisted_columns() -> None:
    """Guard against the r2_keys schema and the PostgresKeyStore INSERT drifting apart."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "004_r2_keys.sql"
    migration_sql = migration_path.read_text()
    for column in ("access_key_id", "owner_user_id", "bucket_name", "access", "alias", "created_at"):
        assert column in migration_sql, f"r2_keys migration is missing column {column!r}"


def test_paid_lists_migration_declares_both_tables() -> None:
    """Guard against the paid_domains / paid_emails schema drifting from the gate queries."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "005_paid_lists.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table paid_domains" in migration_sql
    assert "create table paid_emails" in migration_sql
    for column in ("is_paid", "created_at", "updated_at"):
        assert column in migration_sql, f"paid-lists migration is missing column {column!r}"


def test_slice_name_env_owner_parses_stamped_instance_and_disk_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    assert app_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}") == "dev-josh-foo"
    # The env is recoverable from the data-disk name too (the -data suffix is stripped).
    assert app_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}-data") == "dev-josh-foo"


def test_slice_name_env_owner_returns_none_for_legacy_and_non_slice_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    # Legacy un-stamped slice names have no env owner (must be left untouched).
    assert app_mod.slice_name_env_owner(f"mngr-slice-{host_hex}") is None
    assert app_mod.slice_name_env_owner(f"mngr-slice-{host_hex}-data") is None
    # Non-slice lima names are never attributed to an env.
    assert app_mod.slice_name_env_owner("default") is None
    assert app_mod.slice_name_env_owner("some-other-vm") is None


# -- Workspace sync endpoint tests --


def _make_sync_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemorySyncStore, dict[str, str]]:
    """Create a TestClient with the in-memory sync store installed.

    Returns a mutable ``caller`` holder whose ``user_id`` entry the stubbed
    token-decode reads on every request, so tests can switch the calling user
    without another patch (keeps the monkeypatch ratchet at one occurrence,
    mirroring the bucket-test helper's single-loop pattern).
    """
    client = _make_test_client(monkeypatch)
    store = make_fake_sync_store()
    caller = {"user_id": _ADMIN_STUB_USER_ID}
    sync_fakes: dict[str, object] = {
        "get_sync_store": lambda: store,
        "_get_user_id_from_access_token": lambda token: caller["user_id"],
    }
    for name, fake_impl in sync_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    return client, store, caller


def _sync_record_body(
    host_id: str = "host-aaa111",
    agent_id: str = "agent-bbb222",
    revision: int = 1,
    state: str = "active",
    encrypted_secrets: str | None = None,
) -> dict[str, object]:
    return {
        "host_id": host_id,
        "agent_id": agent_id,
        "display_name": "my workspace",
        "color": "#aabbcc",
        "provider_kind": "lima",
        "hosting_device_id": "device-123",
        "device_label": "joshs-laptop",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": encrypted_secrets,
        "revision": revision,
    }


def test_put_and_list_workspace_records_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    secrets_b64 = base64.b64encode(b"opaque-encrypted-payload").decode("ascii")

    put_resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=secrets_b64),
        headers=_admin_headers(),
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["revision"] == 1

    list_resp = client.get("/sync/records", headers=_admin_headers())
    assert list_resp.status_code == 200
    records = list_resp.json()["records"]
    assert len(records) == 1
    assert records[0]["host_id"] == "host-aaa111"
    assert records[0]["agent_id"] == "agent-bbb222"
    assert records[0]["display_name"] == "my workspace"
    assert records[0]["encrypted_secrets"] == secrets_b64
    assert records[0]["created_at"]


def test_put_workspace_record_rejects_mismatched_path_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put("/sync/records/host-other", json=_sync_record_body(), headers=_admin_headers())
    assert resp.status_code == 400


def test_put_workspace_record_cas_conflict_returns_409_with_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert (
        client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_admin_headers()).status_code == 200
    )

    stale = client.put("/sync/records/host-aaa111", json=_sync_record_body(revision=1), headers=_admin_headers())
    assert stale.status_code == 409
    assert stale.json()["detail"]["stored"]["revision"] == 1

    fresh = client.put("/sync/records/host-aaa111", json=_sync_record_body(revision=2), headers=_admin_headers())
    assert fresh.status_code == 200
    assert fresh.json()["revision"] == 2


def test_second_active_record_for_same_agent_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert (
        client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_admin_headers()).status_code == 200
    )

    conflicting = client.put(
        "/sync/records/host-ccc333",
        json=_sync_record_body(host_id="host-ccc333"),
        headers=_admin_headers(),
    )
    assert conflicting.status_code == 409

    # Tombstoning the first row frees the agent_id for a restored workspace.
    tombstone = _sync_record_body(revision=2, state="destroyed")
    assert client.put("/sync/records/host-aaa111", json=tombstone, headers=_admin_headers()).status_code == 200
    restored = client.put(
        "/sync/records/host-ccc333",
        json=_sync_record_body(host_id="host-ccc333"),
        headers=_admin_headers(),
    )
    assert restored.status_code == 200


def test_scrub_secrets_strips_blobs_but_keeps_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    secrets_b64 = base64.b64encode(b"payload").decode("ascii")
    client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=secrets_b64),
        headers=_admin_headers(),
    )

    scrub = client.post("/sync/scrub-secrets", headers=_admin_headers())
    assert scrub.status_code == 200
    assert scrub.json()["scrubbed"] == 1

    records = client.get("/sync/records", headers=_admin_headers()).json()["records"]
    assert records[0]["encrypted_secrets"] is None
    assert records[0]["display_name"] == "my workspace"


def test_put_workspace_record_rejects_invalid_base64_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets="not-base64!!!"),
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_put_workspace_record_rejects_oversized_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    oversized = base64.b64encode(b"x" * (_MAX_ENCRYPTED_SECRETS_BYTES + 1)).decode("ascii")
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=oversized),
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_put_workspace_record_accepts_empty_provider_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    # minds' create-path seeds a record before discovery knows the provider,
    # so an empty provider_kind must be accepted (enriched by a later push).
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    body = _sync_record_body()
    body["provider_kind"] = ""

    resp = client.put("/sync/records/host-aaa111", json=body, headers=_admin_headers())

    assert resp.status_code == 200
    records = client.get("/sync/records", headers=_admin_headers()).json()["records"]
    assert records[0]["provider_kind"] == ""


def test_put_workspace_record_rejects_unknown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(state="bogus"),
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_sync_records_require_admin_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.get("/sync/records", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_sync_records_are_isolated_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, caller = _make_sync_test_client(monkeypatch)
    client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_admin_headers())

    caller["user_id"] = "other-user-id"
    other_list = client.get("/sync/records", headers=_admin_headers())
    assert other_list.json()["records"] == []
    assert len(store.records_by_key) == 1


def test_key_bundle_round_trip_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert client.get("/sync/bundle", headers=_admin_headers()).status_code == 404

    body = {
        "kdf_salt": base64.b64encode(b"0123456789abcdef").decode("ascii"),
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": base64.b64encode(b"wrapped-dek-bytes").decode("ascii"),
        "key_epoch": 1,
    }
    assert client.put("/sync/bundle", json=body, headers=_admin_headers()).status_code == 200

    fetched = client.get("/sync/bundle", headers=_admin_headers())
    assert fetched.status_code == 200
    assert fetched.json()["wrapped_dek"] == body["wrapped_dek"]
    assert fetched.json()["kdf_salt"] == body["kdf_salt"]
    assert fetched.json()["key_epoch"] == 1

    assert client.delete("/sync/bundle", headers=_admin_headers()).status_code == 200
    assert client.get("/sync/bundle", headers=_admin_headers()).status_code == 404


def test_key_bundle_rejects_oversized_wrapped_dek(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    body = {
        "kdf_salt": base64.b64encode(b"0123456789abcdef").decode("ascii"),
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": base64.b64encode(b"x" * (_MAX_KEY_BUNDLE_FIELD_BYTES + 1)).decode("ascii"),
        "key_epoch": 1,
    }
    assert client.put("/sync/bundle", json=body, headers=_admin_headers()).status_code == 400


def test_delete_workspace_record_removes_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _caller = _make_sync_test_client(monkeypatch)
    client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_admin_headers())

    resp = client.delete("/sync/records/host-aaa111", headers=_admin_headers())
    assert resp.status_code == 200
    assert client.get("/sync/records", headers=_admin_headers()).json()["records"] == []
    # Idempotent: deleting again still succeeds.
    assert client.delete("/sync/records/host-aaa111", headers=_admin_headers()).status_code == 200
    assert len(store.records_by_key) == 0


# -- PostgresSyncStore tests (against the in-memory SQL fake) --


def _make_postgres_sync_store(monkeypatch: pytest.MonkeyPatch) -> tuple[PostgresSyncStore, FakePoolBackend]:
    """Build a PostgresSyncStore whose connections hit the in-memory pool backend."""
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return PostgresSyncStore(), backend


def _store_record(
    host_id: str = "host-aaa111",
    agent_id: str = "agent-1",
    display_name: str = "my-workspace",
    state: str = "active",
    encrypted_secrets: bytes | None = None,
    revision: int = 1,
) -> dict[str, Any]:
    """A store-layer record dict (raw-bytes secrets), as the endpoints hand to put_record."""
    return {
        "host_id": host_id,
        "agent_id": agent_id,
        "display_name": display_name,
        "color": None,
        "provider_kind": "docker",
        "hosting_device_id": "device-1",
        "device_label": "laptop",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": encrypted_secrets,
        "revision": revision,
    }


def test_postgres_sync_store_round_trips_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)

    written = store.put_record("user-1", _store_record(encrypted_secrets=b"\x00opaque-blob"))

    assert written["revision"] == 1
    assert written["encrypted_secrets"] == base64.b64encode(b"\x00opaque-blob").decode("ascii")
    listed = store.list_records("user-1")
    assert [record["host_id"] for record in listed] == ["host-aaa111"]
    assert listed[0]["created_at"] != ""
    assert store.list_records("user-2") == []

    updated = store.put_record("user-1", _store_record(display_name="renamed", revision=2))
    assert updated["display_name"] == "renamed"
    assert updated["revision"] == 2
    # The metadata-only update carried no secrets, so the blob is now gone.
    assert updated["encrypted_secrets"] is None


def test_postgres_sync_store_raises_the_stored_row_on_a_stale_push(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record())

    with pytest.raises(SyncRevisionConflictError) as conflict:
        store.put_record("user-1", _store_record(display_name="stale", revision=1))

    assert conflict.value.stored_record["revision"] == 1
    assert conflict.value.stored_record["display_name"] == "my-workspace"


def test_postgres_sync_store_enforces_one_active_record_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record())

    with pytest.raises(SyncActiveAgentConflictError):
        store.put_record("user-1", _store_record(host_id="host-bbb222"))

    # A tombstone for the same agent on another host is allowed by the partial index.
    tombstone = store.put_record("user-1", _store_record(host_id="host-bbb222", state="destroyed"))
    assert tombstone["state"] == "destroyed"


def test_postgres_sync_store_reports_an_insert_race_as_a_cas_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    store, backend = _make_postgres_sync_store(monkeypatch)
    backend.sync_insert_race_winner = {"user_id": "user-1", **_store_record(display_name="winner")}

    # The loser's INSERT hits the primary key after the winner commits; the
    # retry then reports the race through the regular CAS path.
    with pytest.raises(SyncRevisionConflictError) as conflict:
        store.put_record("user-1", _store_record(display_name="loser"))

    assert conflict.value.stored_record["display_name"] == "winner"


def test_postgres_sync_store_surfaces_a_rowless_update_as_a_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record())
    backend.sync_update_returns_no_row = True

    with pytest.raises(SyncStoreConsistencyError):
        store.put_record("user-1", _store_record(revision=2))


def test_postgres_sync_store_deletes_and_scrubs(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(encrypted_secrets=b"blob"))
    store.put_record("user-1", _store_record(host_id="host-bbb222", agent_id="agent-2"))

    assert store.scrub_secrets("user-1") == 1
    assert all(record["encrypted_secrets"] is None for record in store.list_records("user-1"))
    # A second scrub finds nothing left to strip.
    assert store.scrub_secrets("user-1") == 0

    store.delete_record("user-1", "host-aaa111")
    assert [record["host_id"] for record in store.list_records("user-1")] == ["host-bbb222"]


def test_postgres_sync_store_bundle_crud(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    assert store.get_bundle("user-1") is None

    bundle = {
        "kdf_salt": b"salt-bytes",
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": b"wrapped-dek-bytes",
        "key_epoch": 1,
    }
    store.put_bundle("user-1", bundle)

    fetched = store.get_bundle("user-1")
    assert fetched is not None
    assert fetched["kdf_salt"] == base64.b64encode(b"salt-bytes").decode("ascii")
    assert fetched["wrapped_dek"] == base64.b64encode(b"wrapped-dek-bytes").decode("ascii")
    assert fetched["key_epoch"] == 1

    # The upsert path: a rewrapped bundle replaces the stored one in place.
    store.put_bundle("user-1", {**bundle, "wrapped_dek": b"rewrapped", "key_epoch": 2})
    refetched = store.get_bundle("user-1")
    assert refetched is not None
    assert refetched["key_epoch"] == 2

    store.delete_bundle("user-1")
    assert store.get_bundle("user-1") is None


def test_get_sync_store_returns_a_cached_postgres_store() -> None:
    assert isinstance(get_sync_store(), PostgresSyncStore)
    assert get_sync_store() is get_sync_store()


# ---------------------------------------------------------------------------
# Plans + entitlements tests
# ---------------------------------------------------------------------------


def test_initial_plan_pre_cutoff_paid_email_gets_ally() -> None:
    plan = app_mod._initial_plan_name_for_user(
        "user-1", "alice@imbue.com", time_joined_getter=lambda uid: 0, paid_checker=lambda email: True
    )
    assert plan == "ally"


def test_initial_plan_post_cutoff_paid_email_gets_explorer() -> None:
    """Accounts created after the ship cutoff always start as explorer, paid-listed or not."""
    after_cutoff = app_mod._PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS + 1
    plan = app_mod._initial_plan_name_for_user(
        "user-1", "alice@imbue.com", time_joined_getter=lambda uid: after_cutoff, paid_checker=lambda email: True
    )
    assert plan == "explorer"


def test_initial_plan_unpaid_email_gets_explorer() -> None:
    plan = app_mod._initial_plan_name_for_user(
        "user-1", "bob@gmail.com", time_joined_getter=lambda uid: 0, paid_checker=lambda email: False
    )
    assert plan == "explorer"


def test_ensure_account_entitlements_copies_plan_values_and_is_idempotent() -> None:
    store = make_fake_entitlements_store()
    first = app_mod.ensure_account_entitlements(user_id="user-1", username_prefix="prefix1", email="", store=store)
    assert first.plan_name == "explorer"
    assert first.max_remote_workspaces == EXPLORER_PLAN_VALUES["max_remote_workspaces"]
    # A manual bump survives a second ensure (lazy creation never overwrites).
    store.update_entitlements("user-1", {"max_remote_workspaces": 7})
    second = app_mod.ensure_account_entitlements(user_id="user-1", username_prefix="prefix1", email="", store=store)
    assert second.max_remote_workspaces == 7


def test_ensure_account_entitlements_raises_when_plan_not_seeded() -> None:
    store = InMemoryEntitlementsStore()
    with pytest.raises(app_mod.PlanNotFoundError):
        app_mod.ensure_account_entitlements(user_id="user-1", username_prefix="p", email="", store=store)


# ---------------------------------------------------------------------------
# Tunnel + service quota and hardening tests
# ---------------------------------------------------------------------------


def test_route_create_tunnel_returns_quota_403_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_tunnels=1)
    assert client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_admin_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_tunnels"
    # Idempotent re-create of the existing tunnel is always allowed at the cap.
    assert client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers()).status_code == 200


def test_route_add_service_returns_quota_403_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_services_per_tunnel=1)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    first = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "api", "service_url": "http://localhost:9090"},
        headers=_admin_headers(),
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_services_per_tunnel"
    # Re-adding the existing service (an update) is always allowed at the cap.
    re_add = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:9191"},
        headers=_admin_headers(),
    )
    assert re_add.status_code == 200


def test_route_add_service_agent_auth_respects_owner_quota_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent (tunnel-token) auth resolves the owner's quota via the tunnel-name prefix."""
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_services_per_tunnel=1)
    created = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers()).json()
    agent = _agent_headers(created["tunnel_id"])
    first = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=agent,
    )
    assert first.status_code == 200
    second = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "api", "service_url": "http://localhost:9090"},
        headers=agent,
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_services_per_tunnel"


def test_route_create_tunnel_rejects_identity_less_default_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post(
        "/tunnels",
        json={"agent_id": "agent1", "default_auth_policy": {"rules": []}},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400
    resp = client.post(
        "/tunnels",
        json={
            "agent_id": "agent1",
            "default_auth_policy": {"rules": [{"action": "allow", "include": [{"everyone": {}}]}]},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_route_set_tunnel_auth_rejects_identity_less_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    empty = client.put("/tunnels/testuser--agent1/auth", json={"rules": []}, headers=_admin_headers())
    assert empty.status_code == 400
    everyone = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"everyone": {}}]}]},
        headers=_admin_headers(),
    )
    assert everyone.status_code == 400


def test_route_set_service_auth_rejects_identity_less_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": []}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_validate_auth_policy_accepts_identity_rule_types() -> None:
    policy = AuthPolicy(
        rules=[
            {
                "action": "allow",
                "include": [
                    {"email": {"email": "a@b.com"}},
                    {"email_domain": {"domain": "imbue.com"}},
                    {"login_method": {"id": "idp-1"}},
                    {"group": {"id": "group-1"}},
                ],
            }
        ]
    )
    app_mod.validate_auth_policy_has_identity(policy)


def test_ctx_add_service_rolls_back_on_access_app_failure() -> None:
    """A failed Access Application creation must leave nothing behind (no public exposure)."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("o@x.com"))
    ctx.fake.fail_next_create_access_app = True
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []
    assert ctx.fake.access_apps == {}
    ingress = ctx.fake.tunnel_configs[info.tunnel_id]["config"]["ingress"]
    assert [r for r in ingress if "hostname" in r] == []


def test_ctx_add_service_rolls_back_access_app_on_policy_failure() -> None:
    """A policy-attachment failure must delete the just-created Access App (no policy-less app remains)."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("o@x.com"))
    ctx.fake.fail_next_create_access_policy = True
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []
    assert ctx.fake.access_apps == {}
    ingress = ctx.fake.tunnel_configs[info.tunnel_id]["config"]["ingress"]
    assert [r for r in ingress if "hostname" in r] == []
    # A retry after the transient failure succeeds and attaches the policy.
    retried = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_ids = [a["id"] for a in ctx.fake.access_apps.values() if a["domain"] == retried.hostname]
    assert len(app_ids) == 1
    assert ctx.fake.access_policies[app_ids[0]] != []


def test_ctx_add_service_without_any_policy_is_refused() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(app_mod.ServicePolicyMissingError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []


def test_ctx_create_tunnel_fallback_policy_does_not_clobber_existing_default() -> None:
    """Re-creating a tunnel with a fallback must preserve a user-set default policy."""
    ctx = make_fake_forwarding_ctx()
    user_policy = _email_policy("guest@y.com")
    ctx.create_tunnel("alice", "agent1", default_auth_policy=user_policy)
    fallback = app_mod.owner_email_auth_policy("owner@x.com")
    ctx.create_tunnel("alice", "agent1", fallback_auth_policy=fallback)
    stored = ctx.get_tunnel_auth("alice--agent1")
    assert stored is not None
    assert stored.rules == user_policy.rules


# ---------------------------------------------------------------------------
# Sync quota tests
# ---------------------------------------------------------------------------


def _make_sync_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemorySyncStore, InMemoryEntitlementsStore]:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    store = make_fake_sync_store()
    monkeypatch.setattr(app_mod, "get_sync_store", lambda: store)
    return client, store, entitlements_store


def test_sync_put_active_record_refused_at_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, entitlements_store = _make_sync_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_active_synced_workspaces=1)
    first = client.put(
        "/sync/records/host-1", json=_sync_record_body(host_id="host-1", agent_id="agent-1"), headers=_admin_headers()
    )
    assert first.status_code == 200
    second = client.put(
        "/sync/records/host-2", json=_sync_record_body(host_id="host-2", agent_id="agent-2"), headers=_admin_headers()
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_active_synced_workspaces"
    # Updating the existing active record is always allowed at the cap.
    update = client.put(
        "/sync/records/host-1",
        json=_sync_record_body(host_id="host-1", agent_id="agent-1", revision=2),
        headers=_admin_headers(),
    )
    assert update.status_code == 200
    # Tombstoning is always allowed, and frees quota for a new active record.
    tombstone = client.put(
        "/sync/records/host-1",
        json=_sync_record_body(host_id="host-1", agent_id="agent-1", revision=3, state="destroyed"),
        headers=_admin_headers(),
    )
    assert tombstone.status_code == 200
    third = client.put(
        "/sync/records/host-2", json=_sync_record_body(host_id="host-2", agent_id="agent-2"), headers=_admin_headers()
    )
    assert third.status_code == 200


# ---------------------------------------------------------------------------
# Account endpoint tests
# ---------------------------------------------------------------------------


def test_route_get_account_reports_plan_entitlements_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    litellm.users_by_id[_ADMIN_STUB_USER_ID] = {
        "user_id": _ADMIN_STUB_USER_ID,
        "spend": 12.5,
        "max_budget": 1000.0,
        "budget_reset_at": "2026-08-01T00:00:00Z",
    }
    resp = client.get("/account", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == _ADMIN_STUB_USER_ID
    assert body["email"] == _ADMIN_STUB_EMAIL
    # Stub email is paid-listed + pre-cutoff, so the lazily-created plan is ally.
    assert body["plan_name"] == "ally"
    assert body["entitlements"]["max_remote_workspaces"] == ALLY_PLAN_VALUES["max_remote_workspaces"]
    assert body["usage"]["remote_workspaces"] == 1
    assert body["usage"]["tunnels"] == 1
    assert body["usage"]["llm_spend_usd_this_period"] == 12.5
    assert body["usage"]["llm_budget_resets_at"] == "2026-08-01T00:00:00Z"
    assert sorted(body["available_plans"]) == ["ally", "explorer"]


class _FailForNamedBucketOps(FakeCloudflareOps):
    """FakeCloudflareOps whose usage read fails only for one named bucket."""

    def __init__(self, failing_bucket_name: str) -> None:
        super().__init__()
        self.failing_bucket_name = failing_bucket_name

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        if bucket_name == self.failing_bucket_name:
            raise CloudflareApiError(status_code=500, errors=[{"message": "simulated per-bucket failure"}])
        return super().get_bucket_usage_bytes(bucket_name)


def test_read_bucket_usage_bytes_concurrently_aligns_results_and_errors_positionally() -> None:
    ops = _FailForNamedBucketOps("u1prefix--broken")
    ops.usage_bytes_by_bucket["u1prefix--a"] = 111
    ops.usage_bytes_by_bucket["u1prefix--b"] = 222
    results = app_mod._read_bucket_usage_bytes_concurrently(ops, ["u1prefix--a", "u1prefix--broken", "u1prefix--b"])
    assert results[0] == 111
    assert isinstance(results[1], CloudflareApiError)
    assert results[2] == 222


def test_read_bucket_usage_bytes_concurrently_returns_empty_for_no_buckets() -> None:
    assert app_mod._read_bucket_usage_bytes_concurrently(FakeCloudflareOps(), []) == []


class _BarrierUsageOps(FakeCloudflareOps):
    """FakeCloudflareOps whose usage reads block until all expected readers arrive.

    Proves the reads overlap: sequential reads would deadlock on the barrier
    (surfacing as a BrokenBarrierError after the wait timeout) instead of all
    arriving together.
    """

    def __init__(self, expected_reader_count: int) -> None:
        super().__init__()
        self.reader_barrier = threading.Barrier(expected_reader_count)

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        self.reader_barrier.wait(timeout=10)
        return super().get_bucket_usage_bytes(bucket_name)


def test_read_bucket_usage_bytes_concurrently_overlaps_reads() -> None:
    bucket_count = app_mod._BUCKET_USAGE_MAX_PARALLEL_READS
    ops = _BarrierUsageOps(expected_reader_count=bucket_count)
    bucket_names = [f"u1prefix--bucket{i}" for i in range(bucket_count)]
    for i, name in enumerate(bucket_names):
        ops.usage_bytes_by_bucket[name] = i + 1
    results = app_mod._read_bucket_usage_bytes_concurrently(ops, bucket_names)
    assert results == [i + 1 for i in range(bucket_count)]


def test_measure_live_owner_usage_bytes_raises_when_any_read_fails() -> None:
    ops = _FailForNamedBucketOps("u1prefix--broken")
    ops.buckets["u1prefix--ok"] = {"name": "u1prefix--ok"}
    ops.buckets["u1prefix--broken"] = {"name": "u1prefix--broken"}
    ops.usage_bytes_by_bucket["u1prefix--ok"] = 10
    with pytest.raises(CloudflareApiError):
        app_mod._measure_live_owner_usage_bytes(ops, "u1prefix")


def test_route_set_account_plan_same_plan_is_noop_preserving_bumps(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "ally", max_remote_workspaces=42)
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["entitlements"]["max_remote_workspaces"] == 42
    # No LiteLLM push on a no-op.
    assert litellm.calls == []


def test_route_set_account_plan_switch_overwrites_wholesale(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=42)
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_name"] == "ally"
    # The manual bump is wiped: values reset wholesale to the plan defaults.
    assert body["entitlements"]["max_remote_workspaces"] == ALLY_PLAN_VALUES["max_remote_workspaces"]
    # The new monthly budget is pushed to LiteLLM.
    assert litellm.users_by_id[_ADMIN_STUB_USER_ID]["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]


def test_route_set_account_plan_unknown_plan_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/account/plan", json={"plan": "platinum"}, headers=_admin_headers())
    assert resp.status_code == 400


def test_route_set_account_plan_litellm_failure_aborts_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed LiteLLM budget push fails the whole switch; the row is unchanged."""
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer")
    litellm.fail_user_writes = True
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_admin_headers())
    assert resp.status_code == 500
    row = entitlements_store.get_entitlements(_ADMIN_STUB_USER_ID)
    assert row is not None
    assert row["plan_name"] == "explorer"


def test_get_litellm_user_spend_reports_zero_when_litellm_unreachable() -> None:
    """A transport-level LiteLLM failure degrades the display-only spend to zero (no 500)."""

    def _raise_transport_error(
        method: str, path: str, json_body: dict[str, object] | None = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert app_mod.get_litellm_user_spend("user-1", request_fn=_raise_transport_error) == (0.0, None)


# ---------------------------------------------------------------------------
# Account admin endpoint tests (email-addressed, admin-key authenticated)
# ---------------------------------------------------------------------------


def _make_account_admin_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemoryEntitlementsStore, FakeLiteLLMBackend, FakeSuperTokensBackend]:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    return client, entitlements_store, litellm, st_backend


def test_admin_get_account_lazily_creates_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    resp = client.get("/admin/accounts/somebody@example.com", headers=_paid_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "somebody@example.com"
    assert body["plan_name"] == "explorer"
    assert entitlements_store.get_entitlements(body["user_id"]) is not None


def test_admin_get_account_unknown_email_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, _st_backend = _make_account_admin_test_client(monkeypatch)
    resp = client.get("/admin/accounts/nobody@example.com", headers=_paid_admin_headers())
    assert resp.status_code == 404


def test_admin_account_endpoints_reject_missing_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    # A SuperTokens session token is rejected on the admin-key routes.
    resp = client.get("/admin/accounts/somebody@example.com", headers=_admin_headers())
    assert resp.status_code == 401


def test_admin_set_plan_always_resets_to_plan_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin set-plan resets even for the same plan (the operator's bump-wipe)."""
    client, entitlements_store, litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    show = client.get("/admin/accounts/somebody@example.com", headers=_paid_admin_headers()).json()
    entitlements_store.update_entitlements(show["user_id"], {"max_remote_workspaces": 42})
    resp = client.post(
        "/admin/accounts/somebody@example.com/plan", json={"plan": "explorer"}, headers=_paid_admin_headers()
    )
    assert resp.status_code == 200
    row = entitlements_store.get_entitlements(show["user_id"])
    assert row is not None
    assert row["max_remote_workspaces"] == EXPLORER_PLAN_VALUES["max_remote_workspaces"]
    # Admin set-plan skips the ally eligibility check.
    ally = client.post(
        "/admin/accounts/somebody@example.com/plan", json={"plan": "ally"}, headers=_paid_admin_headers()
    )
    assert ally.status_code == 200
    assert litellm.users_by_id[show["user_id"]]["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]


def test_admin_set_quota_updates_single_value(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    resp = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": 5},
        headers=_paid_admin_headers(),
    )
    assert resp.status_code == 200
    show = client.get("/admin/accounts/somebody@example.com", headers=_paid_admin_headers()).json()
    assert show["entitlements"]["max_remote_workspaces"] == 5
    # Other values are untouched.
    assert show["entitlements"]["max_buckets"] == EXPLORER_PLAN_VALUES["max_buckets"]
    # An LLM budget bump also pushes to LiteLLM.
    resp = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "monthly_llm_spend_usd", "value": 250.5},
        headers=_paid_admin_headers(),
    )
    assert resp.status_code == 200
    assert litellm.users_by_id[show["user_id"]]["max_budget"] == 250.5


def test_admin_set_quota_rejects_bad_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    unknown = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_unicorns", "value": 5},
        headers=_paid_admin_headers(),
    )
    assert unknown.status_code == 400
    fractional = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": 1.5},
        headers=_paid_admin_headers(),
    )
    assert fractional.status_code == 400
    negative = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": -1},
        headers=_paid_admin_headers(),
    )
    assert negative.status_code == 400


# ---------------------------------------------------------------------------
# R2 storage sweep tests
# ---------------------------------------------------------------------------


def _sweep_fixtures() -> tuple[FakeCloudflareOps, InMemoryKeyStore, InMemoryEntitlementsStore]:
    return FakeCloudflareOps(), make_fake_key_store(), make_fake_entitlements_store()


def _run_sweep(
    ops: FakeCloudflareOps,
    store: InMemoryKeyStore,
    entitlements_store: InMemoryEntitlementsStore,
    grant_store: InMemoryGrantStore | None = None,
    email_getter: Callable[[str], str | None] = lambda uid: None,
    only_user_id: str | None = None,
) -> dict[str, int]:
    """Call run_r2_quota_sweep with test defaults (fresh grant store, no-op lock)."""
    return app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        grant_store if grant_store is not None else make_fake_grant_store(),
        email_getter=email_getter,
        enforcement_lock=noop_enforcement_lock,
        only_user_id=only_user_id,
    )


def _add_bucket_with_key(
    ops: FakeCloudflareOps,
    store: InMemoryKeyStore,
    owner_user_id: str,
    bucket_name: str,
    access: str = "readwrite",
    alias: str = "default",
) -> str:
    ops.buckets.setdefault(bucket_name, {"name": bucket_name})
    token = ops.create_bucket_token(bucket_name, access, f"mngr-r2:{bucket_name}:{alias}")
    store.add_key(str(token["id"]), owner_user_id, bucket_name, access, alias)
    return str(token["id"])


def _seed_sweep_row(
    entitlements_store: InMemoryEntitlementsStore, user_id: str, prefix: str, max_total_bucket_bytes: int
) -> None:
    _seed_entitlements_row(
        entitlements_store,
        user_id=user_id,
        username_prefix=prefix,
        max_total_bucket_bytes=max_total_bucket_bytes,
    )


def test_sweep_enforces_single_key_per_bucket() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    first = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    second = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", alias="extra")
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["extra_keys_revoked"] == 1
    remaining = store.list_keys("user-1", "u1prefix--data")
    # The newest key survives; the older one is revoked and dropped.
    assert [r["access_key_id"] for r in remaining] == [second]
    assert first not in ops.account_tokens


def test_sweep_keeps_extra_key_row_when_revoke_fails() -> None:
    """A failed Cloudflare revoke keeps the r2_keys row so the next sweep retries.

    Dropping the row of a still-live token would orphan a credential no later
    sweep could revoke (or downgrade for storage-quota enforcement).
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    first = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    second = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", alias="extra")
    ops.fail_next_delete_bucket_token = True
    failed = _run_sweep(ops, store, entitlements_store)
    assert failed["extra_keys_revoked"] == 0
    assert failed["key_update_failures"] == 1
    # Both the row and the live token survive the failed revoke.
    assert {r["access_key_id"] for r in store.list_keys("user-1", "u1prefix--data")} == {first, second}
    assert first in ops.account_tokens
    # The next (healthy) sweep completes the revoke.
    retried = _run_sweep(ops, store, entitlements_store)
    assert retried["extra_keys_revoked"] == 1
    assert [r["access_key_id"] for r in store.list_keys("user-1", "u1prefix--data")] == [second]
    assert first not in ops.account_tokens


def test_sweep_downgrades_and_restores_keys_around_quota() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000

    over = _run_sweep(ops, store, entitlements_store)
    assert over["users_over_quota"] == 1
    assert over["keys_downgraded"] == 1
    assert ops.account_tokens[key_id]["access"] == "read"
    downgraded_row = store.get_key(key_id)
    assert downgraded_row is not None
    assert downgraded_row["enforced_access"] == "read"

    # Repeated over-quota sweeps are no-ops (already downgraded).
    again = _run_sweep(ops, store, entitlements_store)
    assert again["keys_downgraded"] == 0

    # Back under quota: the key's intended access is restored.
    ops.usage_bytes_by_bucket["u1prefix--data"] = 50
    restored = _run_sweep(ops, store, entitlements_store)
    assert restored["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"
    restored_row = store.get_key(key_id)
    assert restored_row is not None
    assert restored_row["enforced_access"] is None


def test_sweep_never_downgrades_intentionally_read_only_keys() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", access="read")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["keys_downgraded"] == 0
    untouched_row = store.get_key(key_id)
    assert untouched_row is not None
    assert untouched_row["enforced_access"] is None


def test_sweep_sums_usage_across_all_owner_buckets() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 150)
    key_a = _add_bucket_with_key(ops, store, "user-1", "u1prefix--a")
    key_b = _add_bucket_with_key(ops, store, "user-1", "u1prefix--b")
    ops.usage_bytes_by_bucket["u1prefix--a"] = 100
    ops.usage_bytes_by_bucket["u1prefix--b"] = 100
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["users_over_quota"] == 1
    assert counters["keys_downgraded"] == 2
    assert ops.account_tokens[key_a]["access"] == "read"
    assert ops.account_tokens[key_b]["access"] == "read"


def test_sweep_skips_unknown_owner_without_downgrading() -> None:
    """No entitlements row + no resolvable email means skip, never guess a limit."""
    ops, store, entitlements_store = _sweep_fixtures()
    key_id = _add_bucket_with_key(ops, store, "user-unknown", "uxprefix--data")
    ops.usage_bytes_by_bucket["uxprefix--data"] = 10**15
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["users_skipped"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_lazily_creates_row_for_resolvable_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner with no row gets one created from their email (unpaid -> explorer here)."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    # The real time_joined getter degrades to 0 (pre-cutoff) when SuperTokens
    # is unavailable, which is exactly the branch this test wants.
    ops, store, entitlements_store = _sweep_fixtures()
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 10
    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        make_fake_grant_store(),
        email_getter=lambda uid: "nobody@gmail.com",
        enforcement_lock=noop_enforcement_lock,
    )
    assert counters["users_skipped"] == 0
    row = entitlements_store.get_entitlements("user-1")
    assert row is not None
    assert row["plan_name"] == "explorer"
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_confirms_downgrade_against_live_usage() -> None:
    """A stale analytics window peak alone never downgrades: live REST usage is re-checked first.

    This is the anti-flap guarantee: a user who just pruned under quota (peak
    still over, live under) must not have their restored keys re-broken.
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 50
    ops.graphql_usage_bytes_by_bucket = {"u1prefix--data": 1000}
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["downgrades_cancelled_by_live_usage"] == 1
    assert counters["keys_downgraded"] == 0
    assert counters["users_over_quota"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_restores_downgraded_key_when_live_usage_dropped() -> None:
    """A downgraded key is restored as soon as live usage is under quota, even while the peak lags."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    over = _run_sweep(ops, store, entitlements_store)
    assert over["keys_downgraded"] == 1
    # The user cleans up: live usage drops but the window peak still shows the old high-water mark.
    ops.usage_bytes_by_bucket["u1prefix--data"] = 40
    ops.graphql_usage_bytes_by_bucket = {"u1prefix--data": 1000}
    restored = _run_sweep(ops, store, entitlements_store)
    assert restored["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_fails_open_when_live_usage_read_fails() -> None:
    """A failed REST confirmation skips the owner (no downgrade), never enforces on the peak alone."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    ops.fail_bucket_usage_reads = True
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["live_usage_read_failures"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_skips_owner_with_active_grant() -> None:
    """An owner mid-cleanup (active grant) is left alone even when measurably over quota."""
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    counters = _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    assert counters["users_skipped_for_grant"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_skips_downgrade_when_grant_appears_before_lock_acquisition() -> None:
    """A grant created between the loop-top check and the lock must still block the downgrade.

    Simulates the interleave by injecting an enforcement lock that creates
    the grant on entry (a real grant request holds the same lock while it
    restores the keys, so from the sweep's perspective the grant simply
    exists by the time it enters).
    """
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000

    @contextlib.contextmanager
    def grant_creating_lock(owner_user_id: str) -> Iterator[None]:
        grant_store.create_grant(owner_user_id, "u1prefix", 1000, 60)
        yield

    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        grant_store,
        email_getter=lambda uid: None,
        enforcement_lock=grant_creating_lock,
    )
    assert counters["users_skipped_for_grant"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_settles_expired_grants() -> None:
    """A grant whose expiry passed is settled from live usage; decreased usage marks it successful."""
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    grant = grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    ops.usage_bytes_by_bucket["u1prefix--data"] = 400
    grant_store.now_minutes = 61
    counters = _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    assert counters["grants_settled"] == 1
    settled = grant_store.grants_by_id[int(grant["grant_id"])]
    assert settled["settled_bytes"] == 400
    assert settled["is_decreased"] is True
    # Once settled, the owner is enforced normally again (400 > 100 -> downgraded).
    assert counters["keys_downgraded"] == 1


def test_sweep_settles_expired_grant_as_failed_when_usage_did_not_drop() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    grant = grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    grant_store.now_minutes = 61
    _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    settled = grant_store.grants_by_id[int(grant["grant_id"])]
    assert settled["is_decreased"] is False
    assert grant_store.count_failed_grants_in_window("user-1", 24) == 1


def test_sweep_scoped_to_one_user_leaves_others_untouched() -> None:
    """The email-scoped admin sweep only enforces (and revokes extras) for the named owner."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _seed_sweep_row(entitlements_store, "user-2", "u2prefix", 100)
    key_one = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    key_two = _add_bucket_with_key(ops, store, "user-2", "u2prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    ops.usage_bytes_by_bucket["u2prefix--data"] = 1000
    counters = _run_sweep(ops, store, entitlements_store, only_user_id="user-1")
    assert counters["keys_downgraded"] == 1
    assert ops.account_tokens[key_one]["access"] == "read"
    assert ops.account_tokens[key_two]["access"] == "readwrite"


def test_parse_r2_storage_graphql_response_maps_one_row_per_bucket() -> None:
    response = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 100, "metadataSize": 5},
                                "dimensions": {"bucketName": "u1--a"},
                            },
                            {
                                "max": {"payloadSize": 7, "metadataSize": 0},
                                "dimensions": {"bucketName": "u2--b"},
                            },
                        ]
                    }
                ]
            }
        }
    }
    usage = app_mod.parse_r2_storage_graphql_response(response)
    assert usage == {"u1--a": 105, "u2--b": 7}


def test_parse_r2_storage_graphql_response_raises_when_row_budget_is_hit() -> None:
    """A response filling the query's row budget may be truncated and must fail the sweep loudly."""
    full_page = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 1, "metadataSize": 0},
                                "dimensions": {"bucketName": "u1--a"},
                            }
                        ]
                        * app_mod._R2_STORAGE_GRAPHQL_ROW_LIMIT
                    }
                ]
            }
        }
    }
    with pytest.raises(R2StorageResultTruncatedError):
        app_mod.parse_r2_storage_graphql_response(full_page)
    # A small (untruncated) response parses normally.
    assert app_mod.parse_r2_storage_graphql_response({"data": {"viewer": {"accounts": []}}}) == {}


# ---------------------------------------------------------------------------
# Storage creation gate + enforced-at-mint + cleanup grant/recheck endpoints
# ---------------------------------------------------------------------------


def _downgrade_key(fake: FakeCloudflareOps, store: InMemoryKeyStore, access_key_id: str) -> None:
    """Put a key into the sweep's downgraded state (read-only token policy + enforced marker)."""
    fake.account_tokens[access_key_id]["access"] = "read"
    store.set_enforced_access(access_key_id, "read")


def test_create_bucket_over_storage_quota_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    assert client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).status_code == 200
    fake.usage_bytes_by_bucket["testuser--a"] = 1000
    resp = client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_total_bucket_bytes"


def test_create_bucket_storage_check_fails_open_on_usage_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable usage number never blocks creation (missing data never denies)."""
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    assert client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).status_code == 200
    fake.usage_bytes_by_bucket["testuser--a"] = 1000
    fake.fail_bucket_usage_reads = True
    resp = client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    assert resp.status_code == 200


def test_create_bucket_while_enforced_mints_read_only_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh mint must not hand a writable key to an owner the sweep already downgraded."""
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    first = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    _downgrade_key(fake, store, first["key"]["access_key_id"])
    resp = client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"]["access"] == "read"
    assert fake.account_tokens[body["key"]["access_key_id"]]["access"] == "read"
    new_row = store.get_key(body["key"]["access_key_id"])
    assert new_row is not None
    # Intended access stays readwrite so the sweep restores it once under quota.
    assert new_row["access"] == "readwrite"
    assert new_row["enforced_access"] == "read"


def test_roll_key_fresh_mint_respects_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    first = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    second = client.post("/buckets", json={"name": "b"}, headers=_admin_headers()).json()
    _downgrade_key(fake, store, first["key"]["access_key_id"])
    # Revoke b's key so roll-key has to mint a fresh one.
    revoke = client.delete(f"/bucket-keys/{second['key']['access_key_id']}", headers=_admin_headers())
    assert revoke.status_code == 200
    rolled = client.post("/buckets/b/roll-key", headers=_admin_headers())
    assert rolled.status_code == 200
    assert rolled.json()["access"] == "read"


def test_cleanup_grant_not_needed_when_nothing_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store, _entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/account/storage-cleanup-grant", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_needed"
    assert grant_store.grants_by_id == {}


def test_cleanup_grant_restores_keys_and_records_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket["testuser--a"] = 1000
    _downgrade_key(fake, store, key_id)

    resp = client.post("/account/storage-cleanup-grant", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "granted"
    assert body["baseline_bytes"] == 1000
    # The downgraded key is writable again; its intended access is unchanged.
    assert fake.account_tokens[key_id]["access"] == "readwrite"
    restored_row = store.get_key(key_id)
    assert restored_row is not None
    assert restored_row["enforced_access"] is None
    assert len(grant_store.grants_by_id) == 1

    # Idempotent while active: no second grant row is minted.
    again = client.post("/account/storage-cleanup-grant", headers=_admin_headers())
    assert again.status_code == 200
    assert again.json()["status"] == "granted"
    assert len(grant_store.grants_by_id) == 1


def test_cleanup_grant_budget_exhausted_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    _downgrade_key(fake, store, created["key"]["access_key_id"])
    # Burn the failed-grant budget: five grants settled without any decrease.
    for _ in range(5):
        burned = grant_store.create_grant(_ADMIN_STUB_USER_ID, "testuser", 1000, 60)
        grant_store.settle_grant(int(burned["grant_id"]), 1000, False)
    resp = client.post("/account/storage-cleanup-grant", headers=_admin_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "cleanup_grant_budget_exhausted"
    assert detail["limit"] == 5
    assert detail["current"] == 5


def test_storage_recheck_settles_grant_success_and_keeps_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket["testuser--a"] = 1000
    _downgrade_key(fake, store, key_id)
    assert client.post("/account/storage-cleanup-grant", headers=_admin_headers()).status_code == 200
    # The client prunes: usage drops under both the baseline and the limit.
    fake.usage_bytes_by_bucket["testuser--a"] = 40

    resp = client.post("/account/storage-recheck", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["usage_bytes"] == 40
    assert body["is_over_quota"] is False
    assert body["is_grant_settled"] is True
    assert fake.account_tokens[key_id]["access"] == "readwrite"
    settled = list(grant_store.grants_by_id.values())[0]
    assert settled["is_decreased"] is True
    assert grant_store.count_failed_grants_in_window(_ADMIN_STUB_USER_ID, 24) == 0


def test_storage_recheck_redowngrades_and_burns_budget_when_usage_did_not_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket["testuser--a"] = 1000
    _downgrade_key(fake, store, key_id)
    assert client.post("/account/storage-cleanup-grant", headers=_admin_headers()).status_code == 200

    resp = client.post("/account/storage-recheck", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_over_quota"] is True
    assert body["is_grant_settled"] is True
    assert fake.account_tokens[key_id]["access"] == "read"
    assert grant_store.count_failed_grants_in_window(_ADMIN_STUB_USER_ID, 24) == 1


def test_storage_recheck_standalone_restores_without_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who freed space any other way gets restored immediately, no grant involved."""
    client, fake, store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_admin_headers()).json()
    key_id = created["key"]["access_key_id"]
    _downgrade_key(fake, store, key_id)
    fake.usage_bytes_by_bucket["testuser--a"] = 40

    resp = client.post("/account/storage-recheck", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_grant_settled"] is False
    assert body["is_over_quota"] is False
    assert fake.account_tokens[key_id]["access"] == "readwrite"


# ---------------------------------------------------------------------------
# Admin sweep endpoint tests (admin-key authenticated)
# ---------------------------------------------------------------------------


def test_admin_sweep_endpoint_runs_scoped_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    account_user_id = st_backend.accounts_by_email["somebody@example.com"].user_id
    _seed_entitlements_row(
        entitlements_store, user_id=account_user_id, username_prefix="sbprefix", max_total_bucket_bytes=100
    )
    fake.buckets["sbprefix--data"] = {"name": "sbprefix--data"}
    token = fake.create_bucket_token("sbprefix--data", "readwrite", "mngr-r2:sbprefix--data:default")
    store.add_key(str(token["id"]), account_user_id, "sbprefix--data", "readwrite", "default")
    fake.usage_bytes_by_bucket["sbprefix--data"] = 1000

    resp = client.post("/admin/sweep/r2?email=somebody@example.com", headers=_paid_admin_headers())
    assert resp.status_code == 200
    counters = resp.json()["counters"]
    assert counters["keys_downgraded"] == 1
    assert fake.account_tokens[str(token["id"])]["access"] == "read"


def test_admin_sweep_endpoint_rejects_supertokens_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep trigger is operator-key gated; a SuperTokens session must not pass."""
    client, _fake, _store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    resp = client.post("/admin/sweep/r2", headers=_admin_headers())
    assert resp.status_code == 401


def test_cleanup_grants_migration_matches_grant_columns() -> None:
    """Guard against the r2_cleanup_grants schema and the store's column list drifting apart."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "015_r2_cleanup_grants.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table r2_cleanup_grants" in migration_sql
    for column in (name.strip() for name in app_mod._R2_GRANT_COLUMNS.split(",")):
        assert column in migration_sql, f"grant column {column!r} missing from the migration"


def test_plans_migration_declares_all_quota_columns() -> None:
    """Guard against the plans/entitlements schema and QUOTA_ENTITLEMENT_NAMES drifting apart."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "014_plans_entitlements.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table plans" in migration_sql
    assert "create table account_entitlements" in migration_sql
    assert "username_prefix" in migration_sql
    assert "enforced_access" in migration_sql
    for column in app_mod.QUOTA_ENTITLEMENT_NAMES:
        assert migration_sql.count(column) >= 2, f"quota column {column!r} missing from a table"
