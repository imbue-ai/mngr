import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx2
from flask.testing import FlaskClient
from pydantic import AnyUrl

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import SHARED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import SHARED_TEST_WORKSPACE_ID
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import make_shared_share
from imbue.minds.desktop_client.conftest import make_shared_with_me_store_for_test
from imbue.minds.desktop_client.conftest import seed_shared_with_me_listing
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeShareCliInfo
from imbue.minds.desktop_client.shared_with_me_gate import NOT_OWNED_ERROR_CODE
from imbue.minds.desktop_client.shared_with_me_store import SHARED_WITH_ME_DOCUMENT_KIND
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeStore
from imbue.minds.desktop_client.shared_with_me_store import manual_link_doc_id
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_shared_with_me import redeem_session_url
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr_imbue_cloud.wire_types import SUPPORTED_DOCUMENT_FORMAT

_USER_ID = "33333333-3333-3333-3333-333333333333"
_EMAIL = "viewer@example.com"
_OTHER_USER_ID = "44444444-4444-4444-4444-444444444444"
_TEST_KEY = "test-api-key"
_CLIENT_ENV_CONFIG = ClientEnvConfig(
    connector_url=AnyUrl("https://connector.shares.example"),
    litellm_proxy_url=AnyUrl("https://llm.shares.example"),
    accounts_base_url=AnyUrl("https://accounts.shares.example"),
)


class _HostedWorkspacesResolver(StaticBackendResolver):
    """A static resolver whose every known workspace discovery places on one host."""

    hosted_host_id: str = "localhost"

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id in self.list_known_agent_ids():
            return AgentDisplayInfo(agent_name=str(agent_id), host_id=self.hosted_host_id)
        return None


def _build(
    tmp_path: Path,
    cli: FakeImbueCloudCli,
    *,
    http_client: httpx2.Client | None = None,
    is_configured: bool = True,
    # Whether the app has the connector transport; without it only the device-side rows (pasted links) work.
    has_transport: bool = True,
    # Workspace ids this device hosts itself (an owner's own workspace, which the trust gate must never refuse).
    locally_hosted_agent_ids: tuple[str, ...] = (),
    # The host discovery reports for every locally hosted workspace.
    locally_hosted_host_id: str = "localhost",
) -> tuple[FlaskClient, SharedWithMeStore]:
    """An authenticated desktop client with ``_USER_ID`` signed in and a shared-with-me store over ``cli``."""
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    shared_store = make_shared_with_me_store_for_test(tmp_path / "data", cli if has_transport else None)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=_HostedWorkspacesResolver(
            url_by_agent_and_service={agent_id: {} for agent_id in locally_hosted_agent_ids},
            hosted_host_id=locally_hosted_host_id,
        ),
        http_client=http_client,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
        imbue_cloud_cli=cli if has_transport else None,
        session_store=session_store,
        shared_with_me_store=shared_store if is_configured else None,
        client_env_config=_CLIENT_ENV_CONFIG,
    )
    return client, shared_store


def _seeded(
    tmp_path: Path, *shares: SharedWithMeShareCliInfo
) -> tuple[FlaskClient, FakeImbueCloudCli, SharedWithMeStore]:
    """A client whose account's cache already holds ``shares`` (one invited share by default)."""
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(
        cli, _EMAIL, *(shares or (make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID),))
    )
    client, store = _build(tmp_path, cli)
    _refresh(client)
    return client, cli, store


def _with_pasted_link(
    tmp_path: Path, cli: FakeImbueCloudCli, link: str, *, label: str = "", has_transport: bool = True
) -> tuple[FlaskClient, SharedWithMeStore, str]:
    """A client (its health probe answering the gateway's 204) whose account already pasted ``link``, and the row's doc id."""
    client, store = _build(tmp_path, cli, http_client=_health_probe_client(204), has_transport=has_transport)
    added = client.post(f"/ui/api/shared-with-me/links?account={_USER_ID}", json={"link": link, "label": label})
    assert added.status_code == 200, added.data
    return client, store, _body(added)["row"]["share_key"]


def _refresh(client: FlaskClient) -> dict[str, Any]:
    response = client.post(f"/ui/api/shared-with-me/refresh?account={_USER_ID}")
    assert response.status_code == 200, response.data
    return json.loads(response.get_data(as_text=True))


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.get_data(as_text=True))


def _health_probe_client(status_code: int, probed: list[str] | None = None) -> httpx2.Client:
    """An HTTP client whose every request (the pasted link's /_health probe) answers ``status_code``."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if probed is not None:
            probed.append(str(request.url))
        return httpx2.Response(status_code)

    return httpx2.Client(transport=httpx2.MockTransport(_handler))


def _refusal(code: str) -> ImbueCloudShareRefusedCliError:
    refused = ImbueCloudShareRefusedCliError(f"shares respond: {code}")
    refused.code = code
    return refused


def test_list_answers_the_cached_rows_of_the_windows_account(tmp_path: Path) -> None:
    client, _cli, _store = _seeded(
        tmp_path, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID, app_labels={"notes": "nt1"})
    )

    response = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert response.status_code == 200
    body = _body(response)
    assert body["account_id"] == _USER_ID
    (row,) = body["shares"]
    assert row["share_key"] == SHARED_TEST_WORKSPACE_ID
    assert row["host_id"] == SHARED_TEST_HOST_ID
    assert row["status"] == "invited"
    assert row["is_manual"] is False
    assert row["verdict"] is None
    assert [target["app"] for target in row["targets"]] == ["system_interface", "notes"]


def test_list_is_empty_before_any_fetch_and_requires_the_account_to_be_signed_in(tmp_path: Path) -> None:
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli())

    empty = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")
    other = client.get(f"/ui/api/shared-with-me?account={_OTHER_USER_ID}")
    missing = client.get("/ui/api/shared-with-me")

    assert empty.status_code == 200
    assert _body(empty) == {"account_id": _USER_ID, "fetched_at": None, "shares": []}
    assert other.status_code == 409
    assert _body(other)["code"] == "account_not_signed_in"
    assert missing.status_code == 409


def test_routes_require_the_ui_session(tmp_path: Path) -> None:
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli())
    client.delete_cookie(SESSION_COOKIE_NAME)

    response = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert response.status_code == 401


def test_routes_answer_503_when_the_store_is_not_wired(tmp_path: Path) -> None:
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli(), is_configured=False)

    response = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert response.status_code == 503


def test_refresh_reports_a_failed_fetch_and_keeps_the_last_listing(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)
    cli.is_shared_with_me_failing = True

    response = client.post(f"/ui/api/shared-with-me/refresh?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert response.status_code == 502
    assert [row["share_key"] for row in _body(listed)["shares"]] == [SHARED_TEST_WORKSPACE_ID]


def test_open_accepts_the_invitation_and_answers_the_shells_plan(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)

    response = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={})

    assert response.status_code == 200, response.data
    plan = _body(response)
    shell_link = f"https://system-interface-k3j9.{SHARED_TEST_HOST_ID}.ownerlabel.us1.shares.example/"
    assert plan["share_key"] == SHARED_TEST_WORKSPACE_ID
    assert plan["host_id"] == SHARED_TEST_HOST_ID
    assert plan["link"] == shell_link
    assert plan["status"] == "accepted"
    # The redeem URL signs the partition in on the accounts origin, then sends it on to the link.
    redeem = urlsplit(plan["redeem_url"])
    assert (redeem.scheme, redeem.netloc, redeem.path) == ("https", "accounts.shares.example", "/accounts/session")
    assert parse_qs(redeem.query) == {"code": ["code-1"], "next": [shell_link]}
    assert cli.share_responses == [(_EMAIL, SHARED_TEST_HOST_ID, "accepted")]
    assert cli.browser_session_codes_minted == [_EMAIL]


def test_open_mints_the_redeem_code_before_it_records_the_acceptance(tmp_path: Path) -> None:
    """The mint is what the frame waits for, so it takes the one warm mngr process; the acceptance follows it."""
    client, cli, _store = _seeded(tmp_path)

    response = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={})

    assert response.status_code == 200, response.data
    assert cli.shared_with_me_call_order == ["mint", "respond"]
    assert _body(response)["status"] == "accepted"


def test_open_relays_the_connectors_refusal_of_the_acceptance(tmp_path: Path) -> None:
    """The acceptance runs beside the mint, and its refusal is still the open's answer, not a plan."""
    client, cli, _store = _seeded(tmp_path)
    cli.share_response_error_to_raise = _refusal("share_not_found")

    response = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={})

    assert (response.status_code, _body(response)["code"]) == (404, "share_not_found")
    assert cli.shared_with_me_call_order == ["mint", "respond"]


def test_open_of_an_app_uses_that_targets_link_and_keeps_an_accepted_status(tmp_path: Path) -> None:
    client, cli, _store = _seeded(
        tmp_path,
        make_shared_share(
            SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID, status="accepted", app_labels={"notes": "nt1"}
        ),
    )

    response = client.post(
        f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={"app": "notes"}
    )
    unknown = client.post(
        f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={"app": "nothing"}
    )

    assert response.status_code == 200, response.data
    assert _body(response)["link"] == f"https://nt1.{SHARED_TEST_HOST_ID}.ownerlabel.us1.shares.example/"
    # Already accepted: nothing to record.
    assert cli.share_responses == []
    assert unknown.status_code == 409
    assert _body(unknown)["code"] == "no_link"


def test_open_of_a_pasted_link_records_no_response_and_still_signs_the_partition_in(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    link = "https://shell-x.host-q.ownerlabel.us1.shares.example/"
    client, _store, doc_id = _with_pasted_link(tmp_path, cli, link)

    response = client.post(f"/ui/api/shared-with-me/{doc_id}/open?account={_USER_ID}", json={})

    assert response.status_code == 200, response.data
    plan = _body(response)
    assert (plan["share_key"], plan["host_id"], plan["link"], plan["status"]) == (doc_id, "", link, "accepted")
    assert parse_qs(urlsplit(plan["redeem_url"]).query)["next"] == [link]
    # A pasted link has no owner to answer, so nothing reaches the connector.
    assert cli.share_responses == []


def test_open_of_a_declined_share_keeps_the_decline(tmp_path: Path) -> None:
    client, cli, _store = _seeded(
        tmp_path, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID, status="declined")
    )

    response = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={})

    assert response.status_code == 200, response.data
    assert _body(response)["status"] == "declined"
    assert cli.share_responses == []


def test_a_verdict_on_a_pasted_link_keeps_its_entry_a_pasted_link(tmp_path: Path) -> None:
    client, store, doc_id = _with_pasted_link(
        tmp_path, make_fake_imbue_cloud_cli(), "https://x.example/", label="Quinn"
    )

    recorded = client.post(f"/ui/api/shared-with-me/{doc_id}/verdict?account={_USER_ID}", json={"verdict": "offline"})

    assert recorded.status_code == 200, recorded.data
    row = _body(recorded)["row"]
    assert (row["is_manual"], row["workspace_name"], row["verdict"]) == (True, "Quinn", "offline")
    entry = store.documents.read_entries(_USER_ID)[doc_id]
    assert (entry.kind.value, entry.label, entry.link) == ("manual", "Quinn", "https://x.example/")


def test_open_without_a_redeem_code_still_answers_the_link(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)
    cli.is_browser_session_code_failing = True

    response = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/open?account={_USER_ID}", json={})

    assert response.status_code == 200
    assert _body(response)["redeem_url"] is None
    assert _body(response)["link"].startswith("https://system-interface-k3j9.")


def test_open_of_a_pasted_link_needs_no_connector_transport(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    link = "https://shell-x.host-q.ownerlabel.us1.shares.example/"
    client, _store, doc_id = _with_pasted_link(tmp_path, cli, link, has_transport=False)

    opened_link = client.post(f"/ui/api/shared-with-me/{doc_id}/open?account={_USER_ID}", json={})

    assert opened_link.status_code == 200, opened_link.data
    assert (_body(opened_link)["link"], _body(opened_link)["redeem_url"]) == (link, None)
    assert cli.browser_session_codes_minted == []


def test_open_of_an_unknown_key_is_404(tmp_path: Path) -> None:
    client, _cli, _store = _seeded(tmp_path)

    response = client.post(f"/ui/api/shared-with-me/nope/open?account={_USER_ID}", json={})

    assert response.status_code == 404
    assert _body(response)["code"] == "not_shared"


def test_decline_records_the_response_and_undo_clears_it(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)

    declined = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")
    undone = client.delete(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")

    assert declined.status_code == 200, declined.data
    assert _body(declined)["response"] == "declined"
    assert _body(declined)["row"]["status"] == "declined"
    assert undone.status_code == 200, undone.data
    assert _body(undone)["response"] is None
    assert _body(undone)["row"]["status"] == "invited"
    assert cli.share_responses == [(_EMAIL, SHARED_TEST_HOST_ID, "declined"), (_EMAIL, SHARED_TEST_HOST_ID, None)]


def test_a_recorded_response_is_answered_even_when_the_listing_refetch_fails(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)
    # The connector took the response but the follow-up listing fetch did not reach it.
    cli.is_shared_with_me_failing = True

    declined = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")
    # The cache (which the home page is derived from) says what was recorded too.
    listed_after_decline = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")
    undone = client.delete(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")

    assert declined.status_code == 200, declined.data
    assert _body(declined)["row"]["status"] == "declined"
    assert [row["status"] for row in _body(listed_after_decline)["shares"]] == ["declined"]
    assert undone.status_code == 200, undone.data
    assert _body(undone)["row"]["status"] == "invited"
    assert cli.share_responses == [(_EMAIL, SHARED_TEST_HOST_ID, "declined"), (_EMAIL, SHARED_TEST_HOST_ID, None)]


def test_report_drops_the_share_from_the_listing(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)

    reported = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/report?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert reported.status_code == 200, reported.data
    assert _body(reported) == {"share_key": SHARED_TEST_WORKSPACE_ID, "response": "reported", "row": None}
    assert cli.share_responses == [(_EMAIL, SHARED_TEST_HOST_ID, "reported")]
    assert _body(listed)["shares"] == []


def test_a_report_drops_the_share_even_when_the_listing_refetch_fails(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)
    cli.is_shared_with_me_failing = True

    reported = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/report?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert reported.status_code == 200, reported.data
    assert _body(reported)["row"] is None
    assert _body(listed)["shares"] == []


def test_responses_relay_the_connectors_refusal_codes(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)

    cli.share_response_error_to_raise = _refusal("already_reported")
    already = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")
    cli.share_response_error_to_raise = _refusal("rate_limited")
    limited = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/report?account={_USER_ID}")

    assert (already.status_code, _body(already)["code"]) == (409, "already_reported")
    assert (limited.status_code, _body(limited)["code"]) == (429, "rate_limited")


def test_a_share_that_no_longer_names_the_account_answers_404_and_refreshes_the_listing(tmp_path: Path) -> None:
    client, cli, _store = _seeded(tmp_path)
    # The owner revoked the grant since the last fetch: the connector refuses, and the listing is empty now.
    cli.share_response_error_to_raise = _refusal("share_not_found")
    seed_shared_with_me_listing(cli, _EMAIL, version=2)

    gone = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/decline?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert (gone.status_code, _body(gone)["code"]) == (404, "share_not_found")
    assert _body(listed)["shares"] == []


def test_verdict_is_recorded_on_the_row_and_retry_clears_it(tmp_path: Path) -> None:
    client, _cli, store = _seeded(tmp_path)

    recorded = client.post(
        f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/verdict?account={_USER_ID}", json={"verdict": "refused"}
    )
    malformed = client.post(
        f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/verdict?account={_USER_ID}", json={"verdict": "shrug"}
    )
    retried = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/retry?account={_USER_ID}")

    assert recorded.status_code == 200, recorded.data
    assert _body(recorded)["row"]["verdict"] == "refused"
    assert _body(recorded)["row"]["verdict_at"] is not None
    assert malformed.status_code == 400
    # An unknown key is answered before the body is read, as every sibling route answers.
    unknown = client.post(f"/ui/api/shared-with-me/nope/verdict?account={_USER_ID}", json={"verdict": "shrug"})
    assert unknown.status_code == 404
    assert retried.status_code == 200
    assert _body(retried)["row"]["verdict"] is None
    # The verdict lives in the account document, so it follows the account to other devices.
    assert store.documents.list_records(_USER_ID)[0].is_dirty is True


def _newer_app_document(doc_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """A ``shared_with_me`` document as a device on a newer build stores it, in a format this app cannot rewrite."""
    return {
        "kind": SHARED_WITH_ME_DOCUMENT_KIND,
        "doc_id": doc_id,
        "metadata": metadata,
        "encrypted_payload": None,
        "state": "active",
        "revision": 1,
        "document_format": SUPPORTED_DOCUMENT_FORMAT + 1,
    }


def test_an_entry_a_newer_app_wrote_refuses_a_verdict_a_retry_and_a_remove_with_409(tmp_path: Path) -> None:
    client, cli, store = _seeded(tmp_path)
    pasted_link = "https://shell-9.other.us1.shares.example/"
    pasted_doc_id = manual_link_doc_id(pasted_link)
    cli.sync_documents_by_email.setdefault(_EMAIL, {})[SHARED_WITH_ME_DOCUMENT_KIND] = {
        SHARED_TEST_WORKSPACE_ID: _newer_app_document(
            SHARED_TEST_WORKSPACE_ID, {"kind": "listed", "link": "https://x.example/", "last_verdict": "refused"}
        ),
        pasted_doc_id: _newer_app_document(pasted_doc_id, {"kind": "manual", "link": pasted_link, "label": "Nine"}),
    }
    assert store.documents.pull(_USER_ID, _EMAIL).is_reached

    recorded = client.post(
        f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/verdict?account={_USER_ID}", json={"verdict": "admitted"}
    )
    retried = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/retry?account={_USER_ID}")
    removed = client.post(f"/ui/api/shared-with-me/{pasted_doc_id}/remove?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert (recorded.status_code, _body(recorded)["code"]) == (409, "too_new")
    assert (retried.status_code, _body(retried)["code"]) == (409, "too_new")
    assert (removed.status_code, _body(removed)["code"]) == (409, "too_new")
    assert "newer version" in _body(recorded)["error"]
    # The newer entries are read, never rewritten: the verdict stands and the pasted link is still listed.
    rows = {row["share_key"]: row for row in _body(listed)["shares"]}
    assert rows[SHARED_TEST_WORKSPACE_ID]["verdict"] == "refused"
    assert rows[pasted_doc_id]["workspace_name"] == "Nine"
    assert not any(record.is_dirty for record in store.documents.list_records(_USER_ID))


def test_add_link_probes_the_health_route_and_lists_the_pasted_link(tmp_path: Path) -> None:
    probed: list[str] = []
    # The gateway's answer to a probe that carries no gateway session.
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli(), http_client=_health_probe_client(204, probed))
    link = "https://shell-x.host-q.ownerlabel.us1.shares.example/some/path"

    added = client.post(
        f"/ui/api/shared-with-me/links?account={_USER_ID}", json={"link": link, "label": " Quinn's box "}
    )
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert added.status_code == 200, added.data
    row = _body(added)["row"]
    origin = "https://shell-x.host-q.ownerlabel.us1.shares.example/"
    assert probed == [origin + "_health"]
    assert row["is_manual"] is True
    assert row["share_key"] == manual_link_doc_id(origin)
    assert row["workspace_name"] == "Quinn's box"
    assert row["targets"][0]["link"] == origin
    assert [entry["share_key"] for entry in _body(listed)["shares"]] == [row["share_key"]]


def test_add_link_refuses_what_is_not_a_share(tmp_path: Path) -> None:
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli(), http_client=_health_probe_client(404))

    not_a_share = client.post(f"/ui/api/shared-with-me/links?account={_USER_ID}", json={"link": "https://x.example/"})
    not_a_link = client.post(f"/ui/api/shared-with-me/links?account={_USER_ID}", json={"link": "http://x.example/"})
    empty = client.post(f"/ui/api/shared-with-me/links?account={_USER_ID}", json={})

    assert (not_a_share.status_code, _body(not_a_share)["code"]) == (409, "not_a_share")
    assert (not_a_link.status_code, _body(not_a_link)["code"]) == (400, "not_a_link")
    assert empty.status_code == 400


def test_remove_tombstones_a_pasted_link_and_declines_a_listed_share(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(cli, _EMAIL, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID))
    client, _store, doc_id = _with_pasted_link(tmp_path, cli, "https://x.example/")
    _refresh(client)

    removed_link = client.post(f"/ui/api/shared-with-me/{doc_id}/remove?account={_USER_ID}")
    removed_share = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/remove?account={_USER_ID}")
    gone = client.post(f"/ui/api/shared-with-me/{doc_id}/remove?account={_USER_ID}")
    listed = client.get(f"/ui/api/shared-with-me?account={_USER_ID}")

    assert _body(removed_link) == {"share_key": doc_id, "outcome": "removed", "row": None}
    assert _body(removed_share)["outcome"] == "declined"
    assert _body(removed_share)["row"]["status"] == "declined"
    assert gone.status_code == 404
    rows = _body(listed)["shares"]
    assert [(row["share_key"], row["status"]) for row in rows] == [(SHARED_TEST_WORKSPACE_ID, "declined")]
    assert cli.share_responses == [(_EMAIL, SHARED_TEST_HOST_ID, "declined")]


def test_responding_to_a_pasted_link_is_refused(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client, _store, doc_id = _with_pasted_link(tmp_path, cli, "https://x.example/")

    declined = client.post(f"/ui/api/shared-with-me/{doc_id}/decline?account={_USER_ID}")
    undone = client.delete(f"/ui/api/shared-with-me/{doc_id}/decline?account={_USER_ID}")

    assert (declined.status_code, _body(declined)["code"]) == (409, "manual")
    assert (undone.status_code, _body(undone)["code"]) == (409, "manual")
    assert cli.share_responses == []


def _derived_workspaces(client: FlaskClient) -> UiWorkspacesMessage:
    with client.application.app_context():
        publisher = get_state().ui_publisher
    assert publisher is not None
    return publisher.derive_workspaces()


def test_the_ui_workspaces_frame_lists_a_shared_row_under_its_recipient_as_not_owned(tmp_path: Path) -> None:
    # The shared rows ride the same list as the owned ones (specs/shared-with-me, 4.7).
    client, _cli, _store = _seeded(tmp_path)

    entries = _derived_workspaces(client).workspaces

    assert [(entry.id, entry.account_user_id, entry.is_owned) for entry in entries] == [
        (SHARED_TEST_WORKSPACE_ID, _USER_ID, False)
    ]
    assert entries[0].share is not None and entries[0].share.share_key == SHARED_TEST_WORKSPACE_ID
    assert entries[0].host_id == SHARED_TEST_HOST_ID


def test_the_ui_workspaces_frame_lists_nothing_shared_without_a_store(tmp_path: Path) -> None:
    client, _store = _build(tmp_path, make_fake_imbue_cloud_cli(), is_configured=False)

    assert _derived_workspaces(client).workspaces == ()


def test_the_gate_refuses_owner_actions_on_a_listed_share_but_not_its_own_routes(tmp_path: Path) -> None:
    client, _cli, _store = _seeded(tmp_path)

    bearer = {"Authorization": f"Bearer {_TEST_KEY}"}
    renamed = client.post(
        f"/api/v1/workspaces/{SHARED_TEST_WORKSPACE_ID}/rename", json={"name": "mine now"}, headers=bearer
    )
    by_host = client.get(f"/api/v1/machines/{SHARED_TEST_HOST_ID}/sharing", headers=bearer)
    by_workspace_key = client.get(f"/api/v1/workspace-sharing/{SHARED_TEST_WORKSPACE_ID}/readiness", headers=bearer)
    options = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/options")
    # The workspace named in a query, a JSON body, or a bulk list, and on an app-level route.
    by_query = client.get(f"/ui/api/ai-keys?workspace={SHARED_TEST_WORKSPACE_ID}")
    minted = client.post("/settings/ai-keys/mint", json={"workspace": SHARED_TEST_WORKSPACE_ID})
    bulk = client.post("/ui/api/updates/bulk/now", json={"agent_ids": [SHARED_TEST_WORKSPACE_ID]})
    assisted = client.post(
        "/help/assist", json={"description": "help", "workspace_agent_id": SHARED_TEST_WORKSPACE_ID}
    )
    # The handlers parse their bodies whatever the content type says, so the gate must too.
    minted_as_text = client.post(
        "/settings/ai-keys/mint", data=json.dumps({"workspace": SHARED_TEST_WORKSPACE_ID}), content_type="text/plain"
    )
    app_level = client.get(f"/sharing/{SHARED_TEST_WORKSPACE_ID}")
    own_route = client.post(f"/ui/api/shared-with-me/{SHARED_TEST_WORKSPACE_ID}/retry?account={_USER_ID}")
    unrelated = client.get("/ui/api/workspaces/agent-" + "f" * 32 + "/options")

    assert renamed.status_code == 403
    assert _body(renamed)["code"] == NOT_OWNED_ERROR_CODE
    assert by_host.status_code == 403
    assert by_workspace_key.status_code == 403
    assert options.status_code == 403
    assert by_query.status_code == 403
    assert minted.status_code == 403 and _body(minted)["code"] == NOT_OWNED_ERROR_CODE
    assert bulk.status_code == 403
    assert assisted.status_code == 403 and _body(assisted)["code"] == NOT_OWNED_ERROR_CODE
    assert minted_as_text.status_code == 403 and _body(minted_as_text)["code"] == NOT_OWNED_ERROR_CODE
    assert app_level.status_code == 403
    assert own_route.status_code == 200
    assert unrelated.status_code != 403


def test_the_gate_serves_the_page_that_displays_a_shared_workspace(tmp_path: Path) -> None:
    # A reload, a session restore, and "open in a new window" load the
    # workspace page from the server; the page acts on nothing by itself.
    client, _cli, _store = _seeded(tmp_path)

    page = client.get(f"/workspace/{SHARED_TEST_WORKSPACE_ID}")
    options_page = client.get(f"/workspace/{SHARED_TEST_HOST_ID}/options")
    popout_page = client.get(f"/popout/{SHARED_TEST_WORKSPACE_ID}/win-1")

    assert page.status_code != 403
    assert options_page.status_code != 403
    assert popout_page.status_code != 403


def test_the_shared_deep_links_landing_page_is_served(tmp_path: Path) -> None:
    # The shell loads /shared/<host-id> from the server when no window is open.
    client, _cli, _store = _seeded(tmp_path)

    landing = client.get(f"/shared/{SHARED_TEST_HOST_ID}")

    assert landing.status_code not in (403, 404)


def test_the_gate_tells_an_unauthenticated_caller_nothing(tmp_path: Path) -> None:
    # A 403 for a shared coordinate and a 401 for any other would let a caller
    # with no standing here learn which ids are shared with an account.
    client, _cli, _store = _seeded(tmp_path)
    client.delete_cookie(SESSION_COOKIE_NAME)

    renamed = client.post(f"/api/v1/workspaces/{SHARED_TEST_WORKSPACE_ID}/rename", json={"name": "mine now"})
    options = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/options")

    assert renamed.status_code == 401
    assert options.status_code == 401


def test_the_gate_never_refuses_a_workspace_this_device_hosts_itself(tmp_path: Path) -> None:
    # One device can hold both ends of a share: the owner's account hosts the
    # workspace here while another signed-in account is its recipient.
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(cli, _EMAIL, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID))
    client, _store = _build(
        tmp_path, cli, locally_hosted_agent_ids=(SHARED_TEST_WORKSPACE_ID,), locally_hosted_host_id=SHARED_TEST_HOST_ID
    )
    _refresh(client)

    options = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/options")
    # The host discovery reports for the workspace is this device's too.
    by_host = client.get(
        f"/api/v1/machines/{SHARED_TEST_HOST_ID}/sharing", headers={"Authorization": f"Bearer {_TEST_KEY}"}
    )

    assert options.status_code != 403
    assert by_host.status_code != 403


def test_the_gate_never_refuses_a_workspace_an_account_here_holds_a_record_of(tmp_path: Path) -> None:
    # The owner's end of a share may be a record alone (the workspace is not
    # materialized on this device), which discovery never reports.
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_OTHER_USER_ID, email="owner@example.com")
    seed_shared_with_me_listing(cli, _EMAIL, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID))
    client, _store = _build(tmp_path, cli)
    with client.application.app_context():
        session_store = get_state().session_store
    assert session_store is not None
    session_store.associate_created_workspace(
        user_id=_OTHER_USER_ID,
        agent_id=SHARED_TEST_WORKSPACE_ID,
        host_id=SHARED_TEST_HOST_ID,
        display_name="ws",
        color=None,
        is_cloud_row=True,
    )
    _refresh(client)

    options = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/options")
    by_host = client.get(
        f"/api/v1/machines/{SHARED_TEST_HOST_ID}/sharing", headers={"Authorization": f"Bearer {_TEST_KEY}"}
    )

    assert options.status_code != 403
    assert by_host.status_code != 403


def test_redeem_url_is_what_the_shell_expects() -> None:
    assert redeem_session_url("https://accounts.example/", "c0de", "https://x.example/?a=1") == (
        "https://accounts.example/accounts/session?code=c0de&next=https%3A%2F%2Fx.example%2F%3Fa%3D1"
    )
