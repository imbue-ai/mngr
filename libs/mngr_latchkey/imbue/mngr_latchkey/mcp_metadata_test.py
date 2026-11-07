from collections.abc import Mapping

import pytest
from pydantic import JsonValue

from imbue.mngr_latchkey.mcp_metadata import McpLoginVerdictKind
from imbue.mngr_latchkey.mcp_metadata import PublishedAuthorizationServer
from imbue.mngr_latchkey.mcp_metadata import authorization_server_metadata_urls
from imbue.mngr_latchkey.mcp_metadata import describe_published_authorization_servers
from imbue.mngr_latchkey.mcp_metadata import protected_resource_metadata_urls
from imbue.mngr_latchkey.mcp_metadata import published_authorization_servers
from imbue.mngr_latchkey.mcp_metadata import verify_mcp_login

_MCP_ENDPOINT = "https://mcp.trello.com/mcp"
_ISSUER = "https://auth.atlassian.com"
_AUTHORIZE = "https://auth.atlassian.com/authorize"
_TOKEN = "https://auth.atlassian.com/oauth/token"
_REGISTER = "https://auth.atlassian.com/dcr/register"

# What a compliant server publishes: Trello's MCP server delegating to
# Atlassian's authorization server, a layout no domain rule can express.
_PUBLISHED: Mapping[str, JsonValue] = {
    "https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {
        "resource": _MCP_ENDPOINT,
        "authorization_servers": [_ISSUER],
    },
    "https://auth.atlassian.com/.well-known/oauth-authorization-server": {
        "issuer": _ISSUER,
        "authorization_endpoint": _AUTHORIZE,
        "token_endpoint": _TOKEN,
        "registration_endpoint": _REGISTER,
    },
}


class _Fetcher:
    """A scripted fetch that records which URLs were asked for."""

    def __init__(self, documents: Mapping[str, JsonValue]) -> None:
        self.documents = documents
        self.requested: list[str] = []

    def __call__(self, url: str) -> JsonValue | None:
        self.requested.append(url)
        return self.documents.get(url)


def test_protected_resource_metadata_is_looked_for_path_aware_first_then_at_the_root() -> None:
    assert protected_resource_metadata_urls("https://mcp.example.com/mcp") == (
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    )
    # An endpoint at the bare origin has only the root form.
    assert protected_resource_metadata_urls("https://mcp.ticktick.com/") == (
        "https://mcp.ticktick.com/.well-known/oauth-protected-resource",
    )


def test_authorization_server_metadata_is_looked_for_in_the_specified_order() -> None:
    assert authorization_server_metadata_urls("https://auth.example.com") == (
        "https://auth.example.com/.well-known/oauth-authorization-server",
        "https://auth.example.com/.well-known/openid-configuration",
    )
    assert authorization_server_metadata_urls("https://example.com/oauth/") == (
        "https://example.com/.well-known/oauth-authorization-server/oauth",
        "https://example.com/.well-known/openid-configuration/oauth",
        "https://example.com/oauth/.well-known/openid-configuration",
    )


def test_a_matching_sign_in_is_verified_and_only_the_documents_needed_are_read() -> None:
    fetcher = _Fetcher(_PUBLISHED)
    verdict = verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, _TOKEN, _REGISTER, fetcher)
    assert verdict.kind == McpLoginVerdictKind.VERIFIED
    assert verdict.published == (
        PublishedAuthorizationServer(
            issuer=_ISSUER, authorization_endpoint=_AUTHORIZE, token_endpoint=_TOKEN, registration_endpoint=_REGISTER
        ),
    )
    assert fetcher.requested == list(_PUBLISHED)


def test_endpoints_match_case_insensitively_on_the_host_but_exactly_on_the_path() -> None:
    assert (
        verify_mcp_login(
            _MCP_ENDPOINT, "https://AUTH.atlassian.com/authorize", _TOKEN, _REGISTER, _Fetcher(_PUBLISHED)
        ).kind
        == McpLoginVerdictKind.VERIFIED
    )
    assert (
        verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, f"{_TOKEN}/", _REGISTER, _Fetcher(_PUBLISHED)).kind
        == McpLoginVerdictKind.MISMATCH
    )


@pytest.mark.parametrize(
    "login_url,token_endpoint,registration_endpoint",
    [
        # The attack the check exists for: the real sign-in page, a token
        # endpoint of the agent's own.
        (_AUTHORIZE, "https://evil.test/token", _REGISTER),
        (_AUTHORIZE, _TOKEN, "https://evil.test/register"),
        ("https://evil.test/authorize", _TOKEN, _REGISTER),
    ],
)
def test_a_sign_in_the_server_contradicts_is_a_mismatch(
    login_url: str, token_endpoint: str, registration_endpoint: str
) -> None:
    verdict = verify_mcp_login(_MCP_ENDPOINT, login_url, token_endpoint, registration_endpoint, _Fetcher(_PUBLISHED))
    assert verdict.kind == McpLoginVerdictKind.MISMATCH
    assert len(verdict.published) == 1


def test_a_server_without_a_registration_endpoint_can_never_match() -> None:
    documents = dict(_PUBLISHED)
    documents["https://auth.atlassian.com/.well-known/oauth-authorization-server"] = {
        "authorization_endpoint": _AUTHORIZE,
        "token_endpoint": _TOKEN,
    }
    verdict = verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, _TOKEN, _REGISTER, _Fetcher(documents))
    assert verdict.kind == McpLoginVerdictKind.MISMATCH
    assert "registration endpoint none published" in describe_published_authorization_servers(verdict.published)


def test_the_root_resource_metadata_and_openid_metadata_are_fallbacks() -> None:
    documents: Mapping[str, JsonValue] = {
        "https://mcp.trello.com/.well-known/oauth-protected-resource": {"authorization_servers": [_ISSUER]},
        "https://auth.atlassian.com/.well-known/openid-configuration": {
            "authorization_endpoint": _AUTHORIZE,
            "token_endpoint": _TOKEN,
            "registration_endpoint": _REGISTER,
        },
    }
    assert verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, _TOKEN, _REGISTER, _Fetcher(documents)).kind == (
        McpLoginVerdictKind.VERIFIED
    )


def test_the_first_matching_authorization_server_of_several_verifies() -> None:
    other = "https://login.example.com"
    documents = dict(_PUBLISHED)
    documents["https://mcp.trello.com/.well-known/oauth-protected-resource/mcp"] = {
        "authorization_servers": [other, _ISSUER]
    }
    documents[f"{other}/.well-known/oauth-authorization-server"] = {
        "authorization_endpoint": f"{other}/authorize",
        "token_endpoint": f"{other}/token",
        "registration_endpoint": f"{other}/register",
    }
    verdict = verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, _TOKEN, _REGISTER, _Fetcher(documents))
    assert verdict.kind == McpLoginVerdictKind.VERIFIED
    assert [server.issuer for server in verdict.published] == [other, _ISSUER]


@pytest.mark.parametrize(
    "documents",
    [
        # No protected resource metadata at all.
        {},
        # Metadata that names no authorization server.
        {"https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {"resource": _MCP_ENDPOINT}},
        {"https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {"authorization_servers": "nope"}},
        # An authorization server with nothing readable behind it.
        {"https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {"authorization_servers": [_ISSUER]}},
        # Readable, but without its endpoints.
        {
            "https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {"authorization_servers": [_ISSUER]},
            "https://auth.atlassian.com/.well-known/oauth-authorization-server": {"issuer": _ISSUER},
        },
        # Documents that are not objects.
        {"https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": [1, 2]},
    ],
)
def test_nothing_to_check_against_is_unpublished_not_a_refusal(documents: Mapping[str, JsonValue]) -> None:
    assert published_authorization_servers(_MCP_ENDPOINT, _Fetcher(documents)) is None
    verdict = verify_mcp_login(_MCP_ENDPOINT, _AUTHORIZE, _TOKEN, _REGISTER, _Fetcher(documents))
    assert verdict.kind == McpLoginVerdictKind.UNPUBLISHED
    assert verdict.published == ()
