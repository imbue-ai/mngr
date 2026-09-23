import json
from pathlib import Path

from flask.testing import FlaskClient
from pydantic import Field

from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.identity_records import IdentityCache
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import UserIdentityCliInfo


class _ResolvingCli(FakeImbueCloudCli):
    """Fake CLI answering `users resolve` from a canned map (an absent email is a miss)."""

    identity_by_email: dict[str, UserIdentityCliInfo] = Field(default_factory=dict)
    is_resolve_failing: bool = Field(default=False)
    resolve_calls: list[tuple[str, str]] = Field(default_factory=list)

    def resolve_user(self, *, account: str, email: str) -> UserIdentityCliInfo | None:
        self.resolve_calls.append((account, email))
        if self.is_resolve_failing:
            raise ImbueCloudCliError("rate limited")
        return self.identity_by_email.get(email)


def _client(tmp_path: Path, cli: _ResolvingCli, identity_cache: IdentityCache | None) -> FlaskClient:
    cli.accounts_to_return = [
        ImbueCloudAuthAccount(
            user_id="33333333-3333-3333-3333-333333333333", email="owner@example.com", is_active=True
        )
    ]
    store = make_session_store_for_test(tmp_path, cli=cli)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, imbue_cloud_cli=cli, session_store=store, identity_cache=identity_cache
    )
    return client


def test_resolve_user_returns_the_record_and_caches_it(tmp_path: Path) -> None:
    cli = _ResolvingCli(
        connector_url=FAKE_CONNECTOR_URL,
        identity_by_email={
            "bob@example.com": UserIdentityCliInfo(user_id="user-2", email="bob@example.com", display_name="Bob")
        },
    )
    cache = IdentityCache(path=tmp_path / "identity_cache.json")
    client = _client(tmp_path, cli, cache)

    response = client.post("/ui/api/users/resolve", json={"email": " bob@example.com "})

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == {
        "user_id": "user-2",
        "email": "bob@example.com",
        "display_name": "Bob",
        "profile_picture_url": None,
    }
    # The lookup runs under the signed-in account, and the record is cached
    # so the Share tab can render the grant without another round trip.
    assert cli.resolve_calls == [("owner@example.com", "bob@example.com")]
    cached = cache.get("user-2")
    assert cached is not None and cached.record.display_name == "Bob"


def test_resolve_user_answers_404_for_an_unknown_address(tmp_path: Path) -> None:
    client = _client(tmp_path, _ResolvingCli(connector_url=FAKE_CONNECTOR_URL), None)

    response = client.post("/ui/api/users/resolve", json={"email": "nobody@example.com"})

    assert response.status_code == 404


def test_resolve_user_reports_connector_failures_as_502(tmp_path: Path) -> None:
    client = _client(tmp_path, _ResolvingCli(connector_url=FAKE_CONNECTOR_URL, is_resolve_failing=True), None)

    response = client.post("/ui/api/users/resolve", json={"email": "bob@example.com"})

    assert response.status_code == 502


def test_resolve_user_requires_an_email_and_a_session(tmp_path: Path) -> None:
    client = _client(tmp_path, _ResolvingCli(connector_url=FAKE_CONNECTOR_URL), None)
    assert client.post("/ui/api/users/resolve", json={}).status_code == 400

    unauthenticated, _app, _auth_store = build_desktop_client_for_test(tmp_path / "anon", is_authenticated=False)
    assert unauthenticated.post("/ui/api/users/resolve", json={"email": "x@y.z"}).status_code == 401
