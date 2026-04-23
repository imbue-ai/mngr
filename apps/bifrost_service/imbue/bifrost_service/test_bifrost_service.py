"""Integration tests for the bifrost_service FastAPI management API.

Exercise the real endpoint handlers against a fake bifrost admin client (no
running bifrost, no Neon, no network) and a fake SuperTokens dependency. The
fakes are wired in via FastAPI's ``dependency_overrides`` so production code
paths execute end-to-end without any attribute patching at runtime.
"""

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi import Request
from fastapi.testclient import TestClient

from imbue.bifrost_service.app import BifrostAdminClient
from imbue.bifrost_service.app import get_admin_client
from imbue.bifrost_service.app import require_user_prefix
from imbue.bifrost_service.app import web_app
from imbue.bifrost_service.testing import FakeBifrostAdminClient

_USER_A = "aaaaaaaaaaaaaaaa"
_USER_B = "bbbbbbbbbbbbbbbb"


@pytest.fixture
def fake_admin_client() -> FakeBifrostAdminClient:
    return FakeBifrostAdminClient()


@pytest.fixture
def test_app(fake_admin_client: FakeBifrostAdminClient) -> Iterator[None]:
    """Install per-test FastAPI dependency overrides and tear them down after.

    - ``get_admin_client`` always returns the same fake so all callers share
      state (essential for cross-user tests).
    - ``require_user_prefix`` parses ``Authorization: Bearer <prefix>`` and
      returns the bearer value as the user prefix. This means each TestClient
      can pick its own user per request simply by passing its own header,
      without needing a separate FastAPI app instance.
    """

    def _admin_override() -> BifrostAdminClient:
        return fake_admin_client

    def _user_override(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer credentials")
        return header[7:]

    web_app.dependency_overrides[get_admin_client] = _admin_override
    web_app.dependency_overrides[require_user_prefix] = _user_override
    try:
        yield
    finally:
        web_app.dependency_overrides.clear()


def _client_for(user_prefix: str) -> TestClient:
    client = TestClient(web_app)
    client.headers.update({"Authorization": f"Bearer {user_prefix}"})
    return client


@pytest.fixture
def client_as_user_a(test_app: None) -> TestClient:
    del test_app
    return _client_for(_USER_A)


@pytest.fixture
def client_as_user_b(test_app: None) -> TestClient:
    del test_app
    return _client_for(_USER_B)


# --- Create ---


def test_create_key_applies_default_budget_when_not_specified(
    client_as_user_a: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    response = client_as_user_a.post("/keys", json={"name": "my-agent"})

    assert response.status_code == 200
    data = response.json()
    assert data["short_name"] == "my-agent"
    assert data["name"] == f"{_USER_A}--my-agent"
    assert data["value"].startswith("sk-bf-")
    assert data["budget"]["max_limit"] == 100.0
    assert data["budget"]["reset_duration"] == "1d"
    assert fake_admin_client.create_count == 1


def test_create_key_honors_custom_budget(client_as_user_a: TestClient) -> None:
    response = client_as_user_a.post(
        "/keys",
        json={"name": "expensive", "budget_dollars": 500.0, "budget_reset_duration": "1w"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["budget"]["max_limit"] == 500.0
    assert data["budget"]["reset_duration"] == "1w"


def test_create_key_rejects_short_name_containing_separator(client_as_user_a: TestClient) -> None:
    response = client_as_user_a.post("/keys", json={"name": "bad--name"})

    assert response.status_code == 400


def test_create_key_stores_key_under_full_namespaced_name(
    client_as_user_a: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    """The user-facing short name must never leak into bifrost as-is."""
    client_as_user_a.post("/keys", json={"name": "my-agent"})

    stored_names = {r["name"] for r in fake_admin_client.virtual_key_by_id.values()}
    assert stored_names == {f"{_USER_A}--my-agent"}


# --- List ---


def test_list_keys_returns_only_keys_owned_by_caller(
    client_as_user_a: TestClient,
    client_as_user_b: TestClient,
) -> None:
    """Both users write to the same fake DB; each list call must be scoped."""
    client_as_user_a.post("/keys", json={"name": "agent-a1"})
    client_as_user_a.post("/keys", json={"name": "agent-a2"})
    client_as_user_b.post("/keys", json={"name": "agent-b1"})

    response_a = client_as_user_a.get("/keys")
    response_b = client_as_user_b.get("/keys")

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    short_names_a = {k["short_name"] for k in response_a.json()}
    short_names_b = {k["short_name"] for k in response_b.json()}
    assert short_names_a == {"agent-a1", "agent-a2"}
    assert short_names_b == {"agent-b1"}


def test_list_keys_returns_empty_list_when_user_has_no_keys(client_as_user_a: TestClient) -> None:
    response = client_as_user_a.get("/keys")

    assert response.status_code == 200
    assert response.json() == []


# --- Get budget ---


def test_get_key_budget_returns_current_state(client_as_user_a: TestClient) -> None:
    create_response = client_as_user_a.post("/keys", json={"name": "agent", "budget_dollars": 50.0})
    key_id = create_response.json()["key_id"]

    response = client_as_user_a.get(f"/keys/{key_id}/budget")

    assert response.status_code == 200
    data = response.json()
    assert data["max_limit"] == 50.0
    assert data["current_usage"] == 0.0


def test_get_key_budget_reflects_recorded_usage(
    client_as_user_a: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    create_response = client_as_user_a.post("/keys", json={"name": "agent", "budget_dollars": 50.0})
    key_id = create_response.json()["key_id"]
    fake_admin_client.record_usage(key_id, 12.5)

    response = client_as_user_a.get(f"/keys/{key_id}/budget")

    assert response.json()["current_usage"] == 12.5


def test_get_key_budget_returns_404_when_key_missing(client_as_user_a: TestClient) -> None:
    response = client_as_user_a.get("/keys/vk-nonexistent/budget")

    assert response.status_code == 404


def test_get_key_budget_returns_403_when_caller_does_not_own_key(
    client_as_user_a: TestClient,
    client_as_user_b: TestClient,
) -> None:
    """User B must not be able to read User A's key by guessing its ID."""
    create_response = client_as_user_a.post("/keys", json={"name": "agent"})
    key_id = create_response.json()["key_id"]

    response = client_as_user_b.get(f"/keys/{key_id}/budget")

    assert response.status_code == 403


# --- Update budget ---


def test_update_key_budget_changes_limit(client_as_user_a: TestClient) -> None:
    create_response = client_as_user_a.post("/keys", json={"name": "agent"})
    key_id = create_response.json()["key_id"]

    update_response = client_as_user_a.put(
        f"/keys/{key_id}/budget",
        json={"budget_dollars": 250.0, "budget_reset_duration": "1M"},
    )

    assert update_response.status_code == 200
    data = update_response.json()
    assert data["max_limit"] == 250.0
    assert data["reset_duration"] == "1M"


def test_update_key_budget_preserves_existing_usage(
    client_as_user_a: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    """Bumping the limit should not zero out existing spend."""
    create_response = client_as_user_a.post("/keys", json={"name": "agent", "budget_dollars": 100.0})
    key_id = create_response.json()["key_id"]
    fake_admin_client.record_usage(key_id, 40.0)

    update_response = client_as_user_a.put(
        f"/keys/{key_id}/budget",
        json={"budget_dollars": 500.0},
    )

    assert update_response.json()["current_usage"] == 40.0


def test_update_key_budget_returns_403_when_caller_does_not_own_key(
    client_as_user_a: TestClient,
    client_as_user_b: TestClient,
) -> None:
    create_response = client_as_user_a.post("/keys", json={"name": "agent"})
    key_id = create_response.json()["key_id"]

    response = client_as_user_b.put(f"/keys/{key_id}/budget", json={"budget_dollars": 1.0})

    assert response.status_code == 403


# --- Delete ---


def test_delete_key_removes_it_from_bifrost(
    client_as_user_a: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    create_response = client_as_user_a.post("/keys", json={"name": "agent"})
    key_id = create_response.json()["key_id"]

    response = client_as_user_a.delete(f"/keys/{key_id}")

    assert response.status_code == 200
    assert response.json() == {"status": "deleted"}
    assert fake_admin_client.delete_count == 1
    assert key_id not in fake_admin_client.virtual_key_by_id


def test_delete_key_returns_404_when_key_missing(client_as_user_a: TestClient) -> None:
    response = client_as_user_a.delete("/keys/vk-nonexistent")

    assert response.status_code == 404


def test_delete_key_returns_403_when_caller_does_not_own_key(
    client_as_user_a: TestClient,
    client_as_user_b: TestClient,
    fake_admin_client: FakeBifrostAdminClient,
) -> None:
    """A user discovering another user's key_id still cannot delete it."""
    create_response = client_as_user_a.post("/keys", json={"name": "agent"})
    key_id = create_response.json()["key_id"]

    response = client_as_user_b.delete(f"/keys/{key_id}")

    assert response.status_code == 403
    assert fake_admin_client.delete_count == 0
    assert key_id in fake_admin_client.virtual_key_by_id
