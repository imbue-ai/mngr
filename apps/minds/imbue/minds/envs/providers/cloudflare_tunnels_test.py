"""Unit tests for the Cloudflare tunnels provider.

Each test injects an :class:`httpx.MockTransport` via the
``transport`` kwarg so the provider's HTTP layer is exercised
without real network I/O.
"""

import httpx
import pytest
from pydantic import SecretStr

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.providers.cloudflare_tunnels import CloudflareTunnelProviderError
from imbue.minds.envs.providers.cloudflare_tunnels import delete_tunnels
from imbue.minds.envs.providers.cloudflare_tunnels import list_tunnels_for_env


def _ok_list_response(tunnels: list[object], *, total_pages: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": tunnels,
            "result_info": {"total_pages": total_pages},
        },
    )


def test_list_tunnels_for_env_filters_by_metadata_env() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_list_response(
            [
                {"id": "match-1", "metadata": {"env": "dev-josh"}},
                {"id": "other", "metadata": {"env": "different"}},
                {"id": "match-2", "metadata": {"env": "dev-josh"}},
                {"id": "no-metadata"},
                {"id": "non-dict-metadata", "metadata": "weird"},
                "not-a-dict",
            ]
        )

    result = list_tunnels_for_env(
        DevEnvName("dev-josh"),
        account_id="acct-123",
        api_token=SecretStr("token-abc"),
        transport=httpx.MockTransport(handler),
    )
    assert result == ("match-1", "match-2")
    assert captured[0].headers["Authorization"] == "Bearer token-abc"
    assert "accounts/acct-123/cfd_tunnel" in str(captured[0].url)
    assert "is_deleted=false" in str(captured[0].url)


def test_list_tunnels_for_env_paginates() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page=1" in str(request.url):
            return _ok_list_response(
                [{"id": "p1-match", "metadata": {"env": "dev-josh"}}],
                total_pages=2,
            )
        return _ok_list_response(
            [{"id": "p2-match", "metadata": {"env": "dev-josh"}}],
            total_pages=2,
        )

    result = list_tunnels_for_env(
        DevEnvName("dev-josh"),
        account_id="acct",
        api_token=SecretStr("t"),
        transport=httpx.MockTransport(handler),
    )
    assert result == ("p1-match", "p2-match")
    assert len(calls) == 2


def test_list_tunnels_for_env_returns_empty_when_no_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_list_response([{"id": "x", "metadata": {"env": "other"}}])

    assert (
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )
        == ()
    )


def test_list_tunnels_for_env_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server boom")

    with pytest.raises(CloudflareTunnelProviderError, match="500"):
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_list_tunnels_for_env_raises_on_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json{{{")

    with pytest.raises(CloudflareTunnelProviderError, match="non-JSON"):
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_list_tunnels_for_env_raises_on_success_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "errors": [{"message": "nope"}]})

    with pytest.raises(CloudflareTunnelProviderError, match="reported failure"):
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_list_tunnels_for_env_raises_on_non_list_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": "not-a-list"})

    with pytest.raises(CloudflareTunnelProviderError, match="non-list"):
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_list_tunnels_for_env_wraps_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CloudflareTunnelProviderError, match="connection refused"):
        list_tunnels_for_env(
            DevEnvName("dev-josh"),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_delete_tunnels_noop_on_empty_input() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    delete_tunnels(
        (),
        account_id="acct",
        api_token=SecretStr("t"),
        transport=httpx.MockTransport(handler),
    )
    assert calls == []


def test_delete_tunnels_iterates_all_ids() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tunnel_id = str(request.url).rstrip("/").rsplit("/", 1)[-1]
        deleted.append(tunnel_id)
        return httpx.Response(200, json={"success": True})

    delete_tunnels(
        ("a", "b", "c"),
        account_id="acct",
        api_token=SecretStr("t"),
        transport=httpx.MockTransport(handler),
    )
    assert deleted == ["a", "b", "c"]


def test_delete_tunnels_treats_404_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"message": "gone"}]})

    delete_tunnels(
        ("missing-tunnel",),
        account_id="acct",
        api_token=SecretStr("t"),
        transport=httpx.MockTransport(handler),
    )


def test_delete_tunnels_raises_on_non_404_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server boom")

    with pytest.raises(CloudflareTunnelProviderError, match="500"):
        delete_tunnels(
            ("a",),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )


def test_delete_tunnels_wraps_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(CloudflareTunnelProviderError, match="network down"):
        delete_tunnels(
            ("a",),
            account_id="acct",
            api_token=SecretStr("t"),
            transport=httpx.MockTransport(handler),
        )
