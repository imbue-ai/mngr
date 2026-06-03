import pytest

from imbue.minds.desktop_client.sharing_handler import is_probeable_share_url
from imbue.minds.desktop_client.sharing_handler import is_share_ready_from_edge_response


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
