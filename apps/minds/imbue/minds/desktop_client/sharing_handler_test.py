from pathlib import Path

import httpx
import pytest
from pydantic import Field

from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import TunnelInfo
from imbue.minds.desktop_client.sharing_handler import disable_sharing
from imbue.minds.desktop_client.sharing_handler import is_probeable_share_url
from imbue.minds.desktop_client.sharing_handler import is_share_ready_from_edge_response
from imbue.minds.desktop_client.sharing_handler import probe_share_url_readiness
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def test_is_share_ready_from_edge_response_true_for_access_login_redirect() -> None:
    is_ready = is_share_ready_from_edge_response(302, "https://team.cloudflareaccess.com/cdn-cgi/access/login/abc")
    assert is_ready is True


def test_is_share_ready_from_edge_response_true_for_apex_cloudflareaccess_host() -> None:
    is_ready = is_share_ready_from_edge_response(302, "https://cloudflareaccess.com/login")
    assert is_ready is True


@pytest.mark.parametrize(
    "redirect_status_code",
    [301, 303, 307, 308],
)
def test_is_share_ready_from_edge_response_true_for_other_redirect_codes(redirect_status_code: int) -> None:
    is_ready = is_share_ready_from_edge_response(redirect_status_code, "https://team.cloudflareaccess.com/login")
    assert is_ready is True


def test_is_share_ready_from_edge_response_false_for_non_redirect_status() -> None:
    is_ready = is_share_ready_from_edge_response(200, "https://team.cloudflareaccess.com/login")
    assert is_ready is False


def test_is_share_ready_from_edge_response_false_when_location_missing() -> None:
    assert is_share_ready_from_edge_response(302, None) is False
    assert is_share_ready_from_edge_response(302, "") is False


def test_is_share_ready_from_edge_response_false_for_redirect_to_other_host() -> None:
    # A redirect that is not to Cloudflare Access (e.g. the bare origin doing
    # its own redirect) must not be treated as "Access is live".
    is_ready = is_share_ready_from_edge_response(302, "https://example.com/somewhere")
    assert is_ready is False


def test_is_share_ready_from_edge_response_false_for_lookalike_host() -> None:
    # A host that merely ends with the suffix string but is a different domain
    # (no dot boundary) must not match.
    is_ready = is_share_ready_from_edge_response(302, "https://evilcloudflareaccess.com/login")
    assert is_ready is False


def test_is_probeable_share_url_true_for_https_public_hostname() -> None:
    assert is_probeable_share_url("https://web-abc123.tunnels.example.com") is True


def test_is_probeable_share_url_false_for_http_scheme() -> None:
    assert is_probeable_share_url("http://web-abc123.tunnels.example.com") is False


def test_is_probeable_share_url_false_for_empty_or_relative() -> None:
    assert is_probeable_share_url("") is False
    assert is_probeable_share_url("/sharing/agent/web") is False


def test_is_probeable_share_url_false_for_localhost() -> None:
    assert is_probeable_share_url("https://localhost/web") is False
    assert is_probeable_share_url("https://agent-1.localhost/web") is False


@pytest.mark.parametrize(
    "private_url",
    [
        "https://127.0.0.1/web",
        "https://10.0.0.5/web",
        "https://192.168.1.10/web",
        "https://169.254.1.1/web",
    ],
)
def test_is_probeable_share_url_false_for_private_or_loopback_ip(private_url: str) -> None:
    assert is_probeable_share_url(private_url) is False


def test_is_probeable_share_url_true_for_public_ip() -> None:
    assert is_probeable_share_url("https://8.8.8.8/web") is True


# -- disable_sharing idempotency --


class _DisableStubCli(FakeImbueCloudCli):
    """Fake CLI that returns a fixed tunnel and records any service-removal calls."""

    stub_tunnel: TunnelInfo | None = None
    remove_service_calls: list[str] = Field(default_factory=list)

    def find_tunnel_for_agent(self, account: str, agent_id: str) -> TunnelInfo | None:
        return self.stub_tunnel

    def remove_service(self, account: str, tunnel_name: str, service_name: str) -> None:
        self.remove_service_calls.append(service_name)


def test_disable_sharing_is_idempotent_when_service_already_absent(tmp_path: Path) -> None:
    # The tunnel exists but the service is not registered on it (e.g. a repeated
    # disable). Disabling is a no-op success and must never attempt a removal
    # (which would 502 on the connector's 404).
    agent_id = AgentId()
    cli = _DisableStubCli(
        connector_url=FAKE_CONNECTOR_URL,
        stub_tunnel=TunnelInfo(tunnel_name="u--abcd1234efgh5678", tunnel_id="t1", services=()),
    )
    cli.add_account(user_id="u-1", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    store.associate_created_workspace(
        user_id="u-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="",
        color=None,
        is_cloud_row=False,
    )

    disable_sharing(agent_id, ServiceName("web"), cli, store)

    assert cli.remove_service_calls == []


_SHARE_URL = "https://web--abc--owner.example.com"
_LOGIN_URL = "https://team.cloudflareaccess.com/cdn-cgi/access/login/web--abc--owner.example.com"


def _probe_client(login_response: httpx.Response) -> httpx.Client:
    """Client whose edge answers with the Access redirect and whose login URL answers ``login_response``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "team.cloudflareaccess.com":
            return login_response
        return httpx.Response(302, headers={"location": _LOGIN_URL})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_probe_ready_when_edge_redirects_and_login_page_resolves() -> None:
    client = _probe_client(httpx.Response(200, text="<title>Sign in - Cloudflare Access</title>"))
    assert probe_share_url_readiness(client, _SHARE_URL) is True


def test_probe_not_ready_while_access_app_missing_page_shows() -> None:
    # The edge redirect can go live before the Access login service knows the
    # application; its transient error page is a 200, so the probe must read
    # the body to tell it apart from the real login page.
    client = _probe_client(httpx.Response(200, text="<h1>Unable to find your Access application!</h1>"))
    assert probe_share_url_readiness(client, _SHARE_URL) is False


def test_probe_not_ready_when_login_url_errors() -> None:
    client = _probe_client(httpx.Response(500, text="boom"))
    assert probe_share_url_readiness(client, _SHARE_URL) is False


def test_probe_not_ready_without_edge_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert probe_share_url_readiness(client, _SHARE_URL) is False
