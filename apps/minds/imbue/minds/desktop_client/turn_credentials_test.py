import base64
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.mock_turn_credentials_test import ScriptedTurnCredentialClient
from imbue.minds.desktop_client.mock_turn_credentials_test import make_test_credential
from imbue.minds.desktop_client.testing import ManualClock
from imbue.minds.desktop_client.turn_credentials import ConnectorTurnCredentialClient
from imbue.minds.desktop_client.turn_credentials import TURN_ICE_SERVERS_PATH
from imbue.minds.desktop_client.turn_credentials import TurnCredential
from imbue.minds.desktop_client.turn_credentials import TurnCredentialBroker
from imbue.minds.desktop_client.turn_credentials import TurnCredentialRefusedError
from imbue.minds.desktop_client.turn_credentials import TurnCredentialUnavailableError
from imbue.minds.desktop_client.turn_credentials import credential_from_connector_response
from imbue.minds.desktop_client.turn_credentials import is_past_half_life
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.connector.client import CLIENT_ID_HEADER
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.connector.session_store import make_session_from_tokens
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId

_T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
_USER_A = "user-a-" + uuid4().hex
_USER_B = "user-b-" + uuid4().hex
_HOST_A = "host-" + uuid4().hex
_HOST_B = "host-" + uuid4().hex

_CONNECTOR_BODY = {
    "ice_servers": [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {
            "urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turns:turn.cloudflare.com:443?transport=tcp"],
            "username": "cf-user",
            "credential": "cf-secret",
            "future_field": "ignored",
        },
    ],
    "expires_at": "2026-10-17T13:00:00Z",
}


def test_half_life_is_the_midpoint_of_the_credential_lifetime() -> None:
    credential = make_test_credential(_T0)
    assert is_past_half_life(credential, _T0) is False
    assert is_past_half_life(credential, _T0 + timedelta(minutes=29, seconds=59)) is False
    assert is_past_half_life(credential, _T0 + timedelta(minutes=30)) is True
    assert is_past_half_life(credential, _T0 + timedelta(hours=2)) is True


def test_a_200_answer_becomes_a_credential_with_the_servers_unchanged() -> None:
    credential = credential_from_connector_response(200, json.dumps(_CONNECTOR_BODY), _T0)
    assert credential.issued_at == _T0
    assert credential.expires_at == datetime(2026, 10, 17, 13, 0, 0, tzinfo=timezone.utc)
    assert [server.model_dump(exclude_none=True) for server in credential.ice_servers] == [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {
            "urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turns:turn.cloudflare.com:443?transport=tcp"],
            "username": "cf-user",
            "credential": "cf-secret",
        },
    ]


@pytest.mark.parametrize("status_code", [403, 429])
def test_a_structured_refusal_is_a_refusal_naming_its_code(status_code: int) -> None:
    # The connector's refusals are FastAPI HTTPExceptions: a {"detail": {...}} envelope.
    body = json.dumps({"detail": {"code": "turn_quota_exceeded", "limit_bytes": 1, "message": "quota exceeded"}})
    with pytest.raises(TurnCredentialRefusedError, match="turn_quota_exceeded"):
        credential_from_connector_response(status_code, body, _T0)


def test_a_refusal_with_a_plain_string_detail_or_no_json_is_described_by_its_text() -> None:
    with pytest.raises(TurnCredentialRefusedError, match="plan cannot share"):
        credential_from_connector_response(403, json.dumps({"detail": "plan cannot share"}), _T0)
    with pytest.raises(TurnCredentialRefusedError, match="forbidden"):
        credential_from_connector_response(403, "forbidden", _T0)


@pytest.mark.parametrize("status_code", [401, 500, 503])
def test_any_other_failure_is_an_unavailability(status_code: int) -> None:
    with pytest.raises(TurnCredentialUnavailableError, match=str(status_code)):
        credential_from_connector_response(status_code, "", _T0)


def test_an_unreadable_or_expired_200_answer_is_an_unavailability() -> None:
    with pytest.raises(TurnCredentialUnavailableError, match="unreadable"):
        credential_from_connector_response(200, '{"ice_servers": "nope"}', _T0)
    with pytest.raises(TurnCredentialUnavailableError, match="expired"):
        credential_from_connector_response(200, json.dumps(_CONNECTOR_BODY), _T0 + timedelta(hours=2))


def _broker(clock: ManualClock, client: ScriptedTurnCredentialClient) -> TurnCredentialBroker:
    return TurnCredentialBroker(client=client, now_fn=clock)


def test_broker_mints_once_and_serves_the_cached_credential_until_half_life() -> None:
    clock = ManualClock(_T0)
    client = ScriptedTurnCredentialClient(mint_fn=lambda _user_id: make_test_credential(clock()))
    broker = _broker(clock, client)

    first = broker.credential_for_account(_USER_A, _HOST_A)
    clock.advance(29 * 60)
    # A second workspace of the same account shares the cached credential: no second mint.
    again = broker.credential_for_account(_USER_A, _HOST_B)

    assert first is not None and again is first
    assert client.minted_for_workspaces() == [(_USER_A, _HOST_A)]


def test_broker_refreshes_at_half_life_and_caches_per_account() -> None:
    clock = ManualClock(_T0)
    mint_count = {"value": 0}

    def mint(_user_id: str) -> TurnCredential:
        mint_count["value"] += 1
        return make_test_credential(clock(), username=f"u-{mint_count['value']}")

    client = ScriptedTurnCredentialClient(mint_fn=mint)
    broker = _broker(clock, client)

    first = broker.credential_for_account(_USER_A, _HOST_A)
    other = broker.credential_for_account(_USER_B, _HOST_B)
    clock.advance(30 * 60)
    refreshed = broker.credential_for_account(_USER_A, None)

    assert first is not None and other is not None and refreshed is not None
    assert first.ice_servers[1].username == "u-1"
    assert other.ice_servers[1].username == "u-2"
    assert refreshed.ice_servers[1].username == "u-3"
    assert refreshed.issued_at == _T0 + timedelta(minutes=30)
    # Each mint is attributed to the workspace whose read made it, including a read naming none.
    assert client.minted_for_workspaces() == [(_USER_A, _HOST_A), (_USER_B, _HOST_B), (_USER_A, None)]


def test_broker_answers_none_when_the_connector_refuses_and_nothing_is_cached() -> None:
    clock = ManualClock(_T0)

    def refuse(_user_id: str) -> TurnCredential:
        raise TurnCredentialRefusedError("plan cannot share")

    broker = _broker(clock, ScriptedTurnCredentialClient(mint_fn=refuse))
    assert broker.credential_for_account(_USER_A, _HOST_A) is None


def test_broker_keeps_serving_a_cached_credential_through_a_failed_refresh_until_it_expires() -> None:
    clock = ManualClock(_T0)
    state = {"is_failing": False}

    def mint(_user_id: str) -> TurnCredential:
        if state["is_failing"]:
            raise TurnCredentialUnavailableError("connector 503")
        return make_test_credential(clock())

    client = ScriptedTurnCredentialClient(mint_fn=mint)
    broker = _broker(clock, client)
    first = broker.credential_for_account(_USER_A, _HOST_A)
    state["is_failing"] = True

    clock.advance(45 * 60)
    still_served = broker.credential_for_account(_USER_A, _HOST_A)
    clock.advance(16 * 60)
    gone = broker.credential_for_account(_USER_A, _HOST_A)

    assert still_served is first
    assert gone is None
    # Every read past half life tried the connector again.
    assert client.minted_for() == [_USER_A, _USER_A, _USER_A]


def _fake_jwt(expires_at: datetime) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(expires_at.timestamp())}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


def _profile_with_session(tmp_path: Path, user_id: str, access_token: str) -> Path:
    """An mngr host dir whose active profile's session store holds one signed-in account."""
    mngr_host_dir = tmp_path / "mngr"
    profile_id = uuid4().hex
    profile_dir = mngr_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    ImbueCloudSessionStore(sessions_dir=get_sessions_dir(profile_dir)).save(
        make_session_from_tokens(
            user_id=SuperTokensUserId(user_id),
            email=ImbueCloudAccount("alice@example.com"),
            display_name=None,
            access_token=access_token,
            refresh_token=None,
            profile_picture_url=None,
        )
    )
    return mngr_host_dir


def test_connector_client_mints_with_the_session_bearer_and_client_headers(tmp_path: Path) -> None:
    token = _fake_jwt(datetime.now(timezone.utc) + timedelta(hours=6))
    mngr_host_dir = _profile_with_session(tmp_path, _USER_A, token)
    seen_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        body = dict(_CONNECTOR_BODY)
        body["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return httpx.Response(200, json=body)

    client = ConnectorTurnCredentialClient(
        connector_url=AnyUrl("https://connector.example.com/"),
        mngr_host_dir=mngr_host_dir,
        transport=httpx.MockTransport(handle),
    )

    credential = client.mint_credential(_USER_A, _HOST_A)
    client.mint_credential(_USER_A, None)

    attributed, unattributed = seen_requests
    # The workspace's host rides as the query parameter the connector's ledger records.
    assert str(attributed.url) == f"https://connector.example.com{TURN_ICE_SERVERS_PATH}?workspace_id={_HOST_A}"
    assert attributed.headers["Authorization"] == f"Bearer {token}"
    assert attributed.headers[CLIENT_ID_HEADER]
    assert credential.ice_servers[1].username == "cf-user"
    # A mint for no particular workspace sends no parameter at all.
    assert str(unattributed.url) == "https://connector.example.com" + TURN_ICE_SERVERS_PATH


def test_connector_client_reports_no_session_and_an_unreachable_connector_as_unavailable(tmp_path: Path) -> None:
    mngr_host_dir = _profile_with_session(
        tmp_path, _USER_A, _fake_jwt(datetime.now(timezone.utc) + timedelta(hours=6))
    )

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = ConnectorTurnCredentialClient(
        connector_url=AnyUrl("https://connector.example.com"),
        mngr_host_dir=mngr_host_dir,
        transport=httpx.MockTransport(unreachable),
    )
    with pytest.raises(TurnCredentialUnavailableError, match="no session"):
        client.mint_credential(_USER_B, _HOST_A)
    with pytest.raises(TurnCredentialUnavailableError, match="could not reach"):
        client.mint_credential(_USER_A, _HOST_A)
    # An mngr host dir with no profile yet cannot hold a session either.
    bare = ConnectorTurnCredentialClient(
        connector_url=AnyUrl("https://connector.example.com"),
        mngr_host_dir=tmp_path / "empty",
        transport=httpx.MockTransport(unreachable),
    )
    with pytest.raises(TurnCredentialUnavailableError, match="profile"):
        bare.mint_credential(_USER_A, _HOST_A)
