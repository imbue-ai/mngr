"""The two agent-reachable routes the workspace Screen's service reads: media endpoints and ICE servers."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import NETWORK_RESTRICTED_LABEL
from imbue.minds.desktop_client.conftest import _FIXED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.mock_turn_credentials_test import ScriptedTurnCredentialClient
from imbue.minds.desktop_client.mock_turn_credentials_test import make_test_credential
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.testing import ManualClock
from imbue.minds.desktop_client.turn_credentials import TurnCredential
from imbue.minds.desktop_client.turn_credentials import TurnCredentialBroker
from imbue.minds.desktop_client.turn_credentials import TurnCredentialRefusedError
from imbue.mngr.primitives import AgentId

_TEST_KEY = "test-minds-api-key"
_T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
_ACCOUNT_USER_ID = "7c1d4e2f-aaaa-bbbb-cccc-000000000001"
_ACCOUNT_EMAIL = "alice@example.com"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_KEY}"}


def _client(
    tmp_path: Path,
    resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
    broker: TurnCredentialBroker | None = None,
) -> FlaskClient:
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        session_store=session_store,
        turn_credential_broker=broker,
    )
    return app.test_client()


def test_media_endpoints_answers_every_published_endpoint_of_the_workspace_host(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(
        make_agents_json(
            agent_id,
            host_name="ws",
            published_endpoints=[
                {"guest_port": 59000, "protocol": "UDP", "host": "203.0.113.7", "host_port": 24011},
                {"guest_port": 59000, "protocol": "TCP", "host": "203.0.113.7", "host_port": 24011},
            ],
        )
    )
    client = _client(tmp_path, resolver)

    response = client.get(f"/api/v1/agents/{agent_id}/media-endpoints", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {
        "candidates": [
            {"host": "203.0.113.7", "port": 24011, "protocol": "UDP", "guest_port": 59000},
            {"host": "203.0.113.7", "port": 24011, "protocol": "TCP", "guest_port": 59000},
        ]
    }


def test_media_endpoints_is_empty_for_a_host_with_nothing_published(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    client = _client(tmp_path, make_resolver_with_data(make_agents_json(agent_id, host_name="ws")))

    response = client.get(f"/api/v1/agents/{agent_id}/media-endpoints", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"candidates": []}


@pytest.mark.parametrize("route", ["media-endpoints", "ice-servers"])
def test_screen_routes_reject_an_unknown_agent_and_require_the_bearer(tmp_path: Path, route: str) -> None:
    known = AgentId.generate()
    client = _client(tmp_path, make_resolver_with_data(make_agents_json(known, host_name="ws")))

    unknown = client.get(f"/api/v1/agents/{AgentId.generate()}/{route}", headers=_auth_header())
    unauthenticated = client.get(f"/api/v1/agents/{known}/{route}")

    assert unknown.status_code == 404
    assert unauthenticated.status_code == 401


def _owned_workspace(
    tmp_path: Path, labels: dict[str, str] | None = None
) -> tuple[AgentId, BackendResolverInterface, MultiAccountSessionStore]:
    """A workspace an account is signed in for and owns; ``labels`` replaces the default ``is_primary`` label set."""
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(make_agents_json(agent_id, labels=labels, host_name="ws"))
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(_ACCOUNT_USER_ID, _ACCOUNT_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_workspace(_ACCOUNT_USER_ID, str(agent_id), resolver)
    return agent_id, resolver, session_store


def test_ice_servers_answers_the_owning_accounts_credential_and_caches_it(tmp_path: Path) -> None:
    agent_id, resolver, session_store = _owned_workspace(tmp_path)
    clock = ManualClock(_T0)
    turn_client = ScriptedTurnCredentialClient(mint_fn=lambda _user_id: make_test_credential(clock()))
    client = _client(tmp_path, resolver, session_store, TurnCredentialBroker(client=turn_client, now_fn=clock))

    first = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())
    again = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert first.status_code == 200
    body = json.loads(first.data)
    assert body["expires_at"] == "2026-10-17T13:00:00+00:00"
    # The list passes through as the connector answered it: a STUN entry carries no TURN fields.
    assert body["ice_servers"][0] == {"urls": ["stun:stun.cloudflare.com:3478"]}
    assert body["ice_servers"][1]["username"] == "u-1"
    assert body["ice_servers"][1]["credential"] is not None
    assert json.loads(again.data) == body
    # One mint, attributed to the workspace's host for the connector's ledger.
    assert turn_client.minted_for_workspaces() == [(_ACCOUNT_USER_ID, _FIXED_TEST_HOST_ID)]


def test_ice_servers_refuses_a_workspace_whose_host_network_is_restricted(tmp_path: Path) -> None:
    """A relay credential would be an egress tunnel around the host's allowlist, so the broker is never asked."""
    agent_id, resolver, session_store = _owned_workspace(
        tmp_path, labels={"is_primary": "true", NETWORK_RESTRICTED_LABEL: "true"}
    )
    turn_client = ScriptedTurnCredentialClient(mint_fn=lambda _user_id: make_test_credential(_T0))
    client = _client(tmp_path, resolver, session_store, TurnCredentialBroker(client=turn_client))

    response = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"ice_servers": [], "expires_at": None}
    assert turn_client.minted_for() == []


def test_ice_servers_serves_a_workspace_whose_label_is_anything_but_true(tmp_path: Path) -> None:
    agent_id, resolver, session_store = _owned_workspace(
        tmp_path, labels={"is_primary": "true", NETWORK_RESTRICTED_LABEL: "false"}
    )
    turn_client = ScriptedTurnCredentialClient(mint_fn=lambda _user_id: make_test_credential(_T0))
    client = _client(tmp_path, resolver, session_store, TurnCredentialBroker(client=turn_client))

    response = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["ice_servers"][1]["username"] == "u-1"
    assert turn_client.minted_for() == [_ACCOUNT_USER_ID]


def test_ice_servers_is_empty_with_no_account_signed_in_for_the_workspace(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(make_agents_json(agent_id, host_name="ws"))
    turn_client = ScriptedTurnCredentialClient(mint_fn=lambda _user_id: make_test_credential(_T0))
    session_store = make_session_store_for_test(tmp_path / "sessions")
    client = _client(tmp_path, resolver, session_store, TurnCredentialBroker(client=turn_client))

    response = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"ice_servers": [], "expires_at": None}
    assert turn_client.minted_for() == []


def test_ice_servers_is_empty_when_the_connector_refuses(tmp_path: Path) -> None:
    agent_id, resolver, session_store = _owned_workspace(tmp_path)

    def refuse(_user_id: str) -> TurnCredential:
        raise TurnCredentialRefusedError("turn_quota_exceeded")

    client = _client(
        tmp_path, resolver, session_store, TurnCredentialBroker(client=ScriptedTurnCredentialClient(mint_fn=refuse))
    )

    response = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"ice_servers": [], "expires_at": None}


def test_ice_servers_is_empty_in_an_app_built_without_a_broker(tmp_path: Path) -> None:
    agent_id, resolver, session_store = _owned_workspace(tmp_path)
    client = _client(tmp_path, resolver, session_store, broker=None)

    response = client.get(f"/api/v1/agents/{agent_id}/ice-servers", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"ice_servers": [], "expires_at": None}
