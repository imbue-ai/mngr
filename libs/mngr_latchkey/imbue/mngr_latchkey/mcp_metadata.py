"""Checking an ``mcp`` login flow against what the MCP server itself publishes.

An agent asking for a custom service with the ``mcp`` login flow names three
OAuth endpoints: where the browser authorizes, where the authorization code is
exchanged for tokens, and where the OAuth client is registered. The token
endpoint receives the authorization code together with its PKCE verifier, so
one run by a different party than the authorization endpoint steals the
sign-in outright -- and the dialog shows the user only the login page. The
endpoints therefore have to be vouched for by something other than the agent.

The MCP authorization specification provides exactly that. The MCP endpoint's
origin, which is the origin the user approves, publishes *protected resource
metadata* (RFC 9728) naming its authorization servers, and each of those
publishes *authorization server metadata* (RFC 8414, or its OpenID Connect
equivalent) naming its endpoints. Trust is anchored at the approved origin, and
no domain rule is needed: Trello's MCP server delegating to Atlassian's
authorization server is as verifiable as one that signs in at its own host.

This module is pure derivation plus one injected fetch. :func:`verify_mcp_login`
walks the two metadata documents through ``fetch_json`` and returns a
:class:`McpLoginVerdict`: the agent's endpoints match a published authorization
server, they contradict one, or the server publishes nothing to check against.
The last case is not a refusal here -- the caller falls back to the domain rule
in :mod:`imbue.mngr_latchkey.custom_services`, which is what a server that
predates the metadata gets. Fetching lives in the caller because this package
never does network I/O of its own, and because the fake fetch is what makes
the walk testable.
"""

from collections.abc import Callable
from collections.abc import Mapping
from enum import auto
from urllib.parse import urlsplit

from loguru import logger
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

# A fetch of a well-known document: the parsed JSON body on success, ``None``
# when the document is absent, unreachable or not JSON. What ``None`` hides is
# the caller's business; the walk treats every kind the same way.
FetchJson = Callable[[str], JsonValue | None]

PROTECTED_RESOURCE_WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_WELL_KNOWN_PATH = "/.well-known/oauth-authorization-server"
OPENID_CONFIGURATION_WELL_KNOWN_PATH = "/.well-known/openid-configuration"


class PublishedAuthorizationServer(FrozenModel):
    """The endpoints one authorization server publishes, as far as a login flow needs them."""

    issuer: str = Field(description="The authorization server URL the protected resource metadata named.")
    authorization_endpoint: str = Field(description="Where the browser authorizes; the login URL.")
    token_endpoint: str = Field(description="Where the authorization code is exchanged for tokens.")
    registration_endpoint: str | None = Field(
        description="Where OAuth clients are registered, or None if the server publishes none."
    )


class McpLoginVerdictKind(LowerCaseStrEnum):
    """How an ``mcp`` login flow relates to what the MCP server publishes."""

    # The endpoints match one of the server's published authorization servers.
    VERIFIED = auto()
    # The server publishes its authorization servers, and the endpoints match none of them.
    MISMATCH = auto()
    # Nothing to check against: no protected resource metadata, or no reachable
    # authorization server metadata behind it.
    UNPUBLISHED = auto()


class McpLoginVerdict(FrozenModel):
    """The outcome of :func:`verify_mcp_login`."""

    kind: McpLoginVerdictKind = Field(description="Verified, contradicted, or nothing published.")
    published: tuple[PublishedAuthorizationServer, ...] = Field(
        description="Every authorization server whose metadata was read, in the order the MCP server listed them."
    )


def protected_resource_metadata_urls(mcp_endpoint: str) -> tuple[str, ...]:
    """The URLs RFC 9728 metadata for ``mcp_endpoint`` may live at, in the order to try them.

    The path-aware form first (``/.well-known/oauth-protected-resource/mcp`` for
    an endpoint at ``/mcp``), then the origin's root form, which is what the MCP
    specification tells a client to fall back to. An endpoint at the bare origin
    has only the root form.
    """
    parsed = urlsplit(mcp_endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    root = f"{origin}{PROTECTED_RESOURCE_WELL_KNOWN_PATH}"
    if not path:
        return (root,)
    return (f"{root}{path}", root)


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    """The URLs an issuer's RFC 8414 or OpenID metadata may live at, in the order to try them.

    The order the MCP specification gives: the OAuth document, then the OpenID
    one, each with the issuer's path inserted after the well-known segment, and
    for an issuer with a path lastly the OpenID document appended to that path,
    which is where OpenID Connect itself puts it.
    """
    parsed = urlsplit(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [
        f"{origin}{AUTHORIZATION_SERVER_WELL_KNOWN_PATH}{path}",
        f"{origin}{OPENID_CONFIGURATION_WELL_KNOWN_PATH}{path}",
    ]
    if path:
        candidates.append(f"{origin}{path}{OPENID_CONFIGURATION_WELL_KNOWN_PATH}")
    return tuple(candidates)


def _first_object(fetch_json: FetchJson, urls: tuple[str, ...]) -> Mapping[str, JsonValue] | None:
    for url in urls:
        document = fetch_json(url)
        if isinstance(document, dict):
            return document
    return None


def _string_field(document: Mapping[str, JsonValue], name: str) -> str | None:
    value = document.get(name)
    return value if isinstance(value, str) and value else None


def published_authorization_servers(
    mcp_endpoint: str, fetch_json: FetchJson
) -> tuple[PublishedAuthorizationServer, ...] | None:
    """Read the authorization servers ``mcp_endpoint`` publishes, or ``None`` if it publishes none.

    ``None`` when there is no protected resource metadata, when it names no
    authorization server, or when none of the named servers has readable
    metadata of its own: in each case there is nothing to check a login flow
    against. A server whose metadata lacks an endpoint is skipped, not refused;
    the caller compares against what was readable.
    """
    resource_metadata = _first_object(fetch_json, protected_resource_metadata_urls(mcp_endpoint))
    if resource_metadata is None:
        return None
    issuers = resource_metadata.get("authorization_servers")
    if not isinstance(issuers, list):
        return None
    published: list[PublishedAuthorizationServer] = []
    for issuer in issuers:
        if not isinstance(issuer, str) or not issuer:
            continue
        server_metadata = _first_object(fetch_json, authorization_server_metadata_urls(issuer))
        if server_metadata is None:
            logger.info("Authorization server {} named by {} publishes no readable metadata", issuer, mcp_endpoint)
            continue
        authorization_endpoint = _string_field(server_metadata, "authorization_endpoint")
        token_endpoint = _string_field(server_metadata, "token_endpoint")
        if authorization_endpoint is None or token_endpoint is None:
            logger.info("Authorization server {} publishes metadata without its endpoints", issuer)
            continue
        published.append(
            PublishedAuthorizationServer(
                issuer=issuer,
                authorization_endpoint=authorization_endpoint,
                token_endpoint=token_endpoint,
                registration_endpoint=_string_field(server_metadata, "registration_endpoint"),
            )
        )
    return tuple(published) if published else None


def _normalized_url(url: str) -> tuple[str, str, str, str]:
    """The parts of ``url`` that identify an endpoint: scheme and host case-insensitively, path and query as given.

    A trailing slash is kept significant -- latchkey sends requests to the
    endpoint exactly as registered, so a flow has to carry the endpoint exactly
    as published.
    """
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query)


def _is_same_endpoint(requested: str, published: str | None) -> bool:
    return published is not None and _normalized_url(requested) == _normalized_url(published)


def verify_mcp_login(
    mcp_endpoint: str,
    login_url: str,
    token_endpoint: str,
    registration_endpoint: str,
    fetch_json: FetchJson,
) -> McpLoginVerdict:
    """Check the three endpoints of an ``mcp`` login flow against what ``mcp_endpoint`` publishes.

    Verified when some published authorization server has exactly these three
    endpoints (the login URL being its authorization endpoint); a mismatch when
    servers are published and none has them; unpublished when there is nothing
    to compare against. A published server without a registration endpoint can
    never match, since the flow registers a client there.
    """
    published = published_authorization_servers(mcp_endpoint, fetch_json)
    if published is None:
        return McpLoginVerdict(kind=McpLoginVerdictKind.UNPUBLISHED, published=())
    for server in published:
        if (
            _is_same_endpoint(login_url, server.authorization_endpoint)
            and _is_same_endpoint(token_endpoint, server.token_endpoint)
            and _is_same_endpoint(registration_endpoint, server.registration_endpoint)
        ):
            return McpLoginVerdict(kind=McpLoginVerdictKind.VERIFIED, published=published)
    return McpLoginVerdict(kind=McpLoginVerdictKind.MISMATCH, published=published)


def describe_published_authorization_servers(published: tuple[PublishedAuthorizationServer, ...]) -> str:
    """One line per published authorization server, for the message that tells an agent what to re-request with."""
    lines = []
    for server in published:
        registration = server.registration_endpoint or "none published"
        lines.append(
            f"{server.issuer}: authorization endpoint {server.authorization_endpoint}, "
            f"token endpoint {server.token_endpoint}, registration endpoint {registration}"
        )
    return "\n".join(lines)
