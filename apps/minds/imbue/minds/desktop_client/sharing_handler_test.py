from pathlib import Path

import httpx
import pytest

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import _parse_grants_toml
from imbue.minds.desktop_client.sharing_handler import describe_connector_failure
from imbue.minds.desktop_client.sharing_handler import probe_share_readiness
from imbue.minds.desktop_client.sharing_handler import resolve_agent_for_host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.shares.example"


def test_probe_share_readiness_true_on_any_http_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{_DOMAIN}/"
        return httpx.Response(302, headers={"location": "https://accounts.example/share/authorize?x=1"})

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_true_even_on_403() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_false_on_transport_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("relay not reachable")

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is False


def test_grants_toml_roundtrips_through_render_and_parse() -> None:
    workspace_grants = {"emails": ["bob@example.com"], "email_domains": ["partner.org"]}
    service_grants = {
        "web": {"emails": ["carol@example.com"], "email_domains": []},
        "my-app": {"emails": [], "email_domains": ["viewer.dev"]},
    }

    rendered = render_grants_toml(workspace_grants, service_grants)
    parsed = _parse_grants_toml(rendered)

    assert parsed is not None
    parsed_workspace, parsed_services = parsed
    assert parsed_workspace == workspace_grants
    assert parsed_services == service_grants


def test_parse_grants_toml_reports_malformation_as_none() -> None:
    # Malformed must stay distinguishable from empty: rendered as "no grants",
    # the next whole-document save would erase every real grant.
    assert _parse_grants_toml("not toml [[") is None


def test_parse_grants_toml_tolerates_wrong_shapes_as_empty() -> None:
    # Valid TOML of the wrong shape parses to an empty scope (there is no
    # hidden grant to protect: the value's meaning is unambiguous, just wrong).
    parsed = _parse_grants_toml("workspace = 'not-a-table'")
    assert parsed is not None
    workspace_grants, service_grants = parsed
    assert workspace_grants == {"emails": [], "email_domains": []}
    assert service_grants == {}


def test_describe_connector_failure_reports_an_expired_session() -> None:
    # Not "signed out": the account is still in this device's credential list,
    # so the app goes on showing it as signed in.
    exc = ImbueCloudCliError("shares create failed: Refresh rejected by connector: Session missing in db")
    message = describe_connector_failure(exc)
    assert message == "Your Imbue Cloud session has expired. You may need to log out and log in again."
    assert "signed out" not in message


def test_describe_connector_failure_reports_an_unverified_email() -> None:
    exc = ImbueCloudCliError('sync records push failed: Unauthenticated (401): {"detail":"Email not verified"}')
    assert describe_connector_failure(exc) == (
        "Imbue Cloud has not verified this account's email address. Verify it, then retry."
    )


def test_describe_connector_failure_keeps_an_unrecognized_message() -> None:
    # Better the connector's own wording than a pointer to a log file.
    exc = ImbueCloudCliError("shares create failed: Connector error 500: upstream exploded")
    assert describe_connector_failure(exc) == "shares create failed: Connector error 500: upstream exploded"


def test_resolve_agent_for_host_falls_back_to_the_workspace_record(tmp_path: Path) -> None:
    """A stopped (undiscovered) machine still resolves via its active workspace record.

    Without the fallback, the Share pane of a stopped machine read as "not
    shared" and disable returned 502 even while a connector share was active.
    """
    agent_id = AgentId.generate()
    host_id = str(HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-rec-1", email="rec@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)
    store.associate_created_workspace(
        user_id="user-rec-1",
        agent_id=str(agent_id),
        host_id=host_id,
        display_name="stopped-machine",
        color=None,
        is_cloud_row=False,
    )
    undiscovered = StaticBackendResolver(url_by_agent_and_service={})

    assert resolve_agent_for_host(undiscovered, host_id, store) == agent_id


def test_resolve_agent_for_host_raises_when_neither_discovery_nor_records_know_the_host(tmp_path: Path) -> None:
    store = make_session_store_for_test(tmp_path, cli=make_fake_imbue_cloud_cli())
    undiscovered = StaticBackendResolver(url_by_agent_and_service={})

    with pytest.raises(SharingError, match="No workspace is known"):
        resolve_agent_for_host(undiscovered, str(HostId.generate()), store)
