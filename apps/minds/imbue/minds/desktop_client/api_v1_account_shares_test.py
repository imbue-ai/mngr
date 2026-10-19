import json
from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import associate_hosted_workspace
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_USER_ID = "44444444-4444-4444-4444-444444444444"
_EMAIL = "sharer@example.com"
_SHARES_URL = f"/api/v1/account-shares/{_USER_ID}"


def _api_paths(tmp_path: Path) -> InstallationPaths:
    """The install paths that mount the ``/api/v1`` blueprint on the test app."""
    return InstallationPaths(data_dir=tmp_path / "minds")


def _signed_in_client(
    tmp_path: Path, resolver: BackendResolverInterface, cli: FakeImbueCloudCli
) -> tuple[FlaskClient, MultiAccountSessionStore]:
    """A cookie-authenticated client whose session store has the test account signed in on ``cli``."""
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        imbue_cloud_cli=cli,
        session_store=store,
        paths=_api_paths(tmp_path),
    )
    return client, store


def _hosted_workspace(store: MultiAccountSessionStore, agent_id: AgentId, resolver: BackendResolverInterface) -> str:
    """Associate the discovered workspace ``agent_id`` with the test account; returns its host id."""
    return associate_hosted_workspace(store, agent_id, resolver, _USER_ID)


def test_list_account_shares_joins_each_share_to_the_accounts_records(tmp_path: Path) -> None:
    live_agent_id = AgentId()
    gone_host, orphan_host = "host-" + "b" * 32, "host-" + "c" * 32
    cli = make_fake_imbue_cloud_cli()
    resolver = make_resolver_with_data(make_agents_json(live_agent_id))
    client, store = _signed_in_client(tmp_path, resolver, cli)
    live_host = _hosted_workspace(store, live_agent_id, resolver)
    assert store.record_store is not None
    store.record_store.upsert_local_record(
        _USER_ID,
        _EMAIL,
        ReplicaRecord(host_id=gone_host, agent_id="agent-gone", display_name="beta", state="destroyed"),
    )
    for host_id, workspace_id in (
        (live_host, str(live_agent_id)),
        (gone_host, "agent-gone"),
        (orphan_host, "agent-orphan"),
    ):
        cli.add_share(_EMAIL, host_id)
        cli.share_details_by_host_id[host_id] = ShareCliInfo(
            host_id=host_id,
            workspace_id=workspace_id,
            workspace_domain=f"{workspace_id}.owner.us1.shares.example",
            region="us1",
            state="active",
            created_at="2026-09-01 10:00:00+00:00",
        )

    response = client.get(_SHARES_URL)

    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert (body["account_id"], body["email"]) == (_USER_ID, _EMAIL)
    by_host = {row["host_id"]: row for row in body["rows"]}
    assert by_host[live_host]["display_name"] == "alpha"
    assert by_host[live_host]["record_state"] == "active"
    assert by_host[live_host]["is_hosted_here"] is True
    assert by_host[live_host]["is_likely_leaked"] is False
    assert by_host[gone_host]["record_state"] == "destroyed"
    assert by_host[gone_host]["is_likely_leaked"] is True
    assert by_host[orphan_host]["record_state"] == "missing"
    assert by_host[orphan_host]["display_name"] == "agent-orphan.owner.us1.shares.example"
    assert by_host[orphan_host]["last_seen_display"] == "Never connected"
    assert by_host[orphan_host]["created_display"] == "2026-09-01"


def test_list_account_shares_404s_for_an_account_not_signed_in_here(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        paths=_api_paths(tmp_path),
    )

    response = client.get(_SHARES_URL)

    assert response.status_code == 404


def test_account_shares_routes_require_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=False,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        paths=_api_paths(tmp_path),
    )

    assert client.get(_SHARES_URL).status_code == 401
    assert client.delete(f"{_SHARES_URL}/host-{'a' * 32}").status_code == 401


def test_remove_account_share_deletes_a_share_whose_workspace_is_elsewhere(tmp_path: Path) -> None:
    gone_host = "host-" + "b" * 32
    cli = make_fake_imbue_cloud_cli()
    cli.add_share(_EMAIL, gone_host)
    client, _store = _signed_in_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}), cli)

    response = client.delete(f"{_SHARES_URL}/{gone_host}")

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {"host_id": gone_host, "outcome": "removed"}
    assert cli.deleted_share_host_ids == [gone_host]
    assert json.loads(client.get(_SHARES_URL).data)["rows"] == []


def test_remove_account_share_unpublishes_a_workspace_this_device_runs(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = make_fake_imbue_cloud_cli()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client, store = _signed_in_client(tmp_path, resolver, cli)
    host_id = _hosted_workspace(store, agent_id, resolver)
    cli.add_share(_EMAIL, host_id)

    response = client.delete(f"{_SHARES_URL}/{host_id}")

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {"host_id": host_id, "outcome": "stopped_sharing"}
    assert cli.deleted_share_host_ids == [host_id]
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    assert any(call and call[0] == "exec" and "share.env" in call[2] for call in caller.calls)
