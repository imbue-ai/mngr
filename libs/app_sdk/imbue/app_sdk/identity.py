import urllib.parse
from typing import Final

from pydantic import ConfigDict
from pydantic import Field

from imbue.app_sdk.errors import InvalidIdentityHeaderError
from imbue.app_sdk.frozen_model import FrozenModel
from imbue.app_sdk.primitives import OWNER_PARTICIPANT_KEY
from imbue.app_sdk.primitives import ParticipantKey
from imbue.app_sdk.primitives import UserId

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for: the owner flag always; the account (its id and email) when the workspace is
    shared; the grant (its agent id, name, and granter) for a visiting agent. What to call an account and what it
    looks like is its profile, not the header's."""

    # The header is cross-version wire data from the proxies; a field this build does not
    # know -- or no longer reads, like the display name older proxies stamp -- must never make it unreadable.
    model_config = ConfigDict(extra="ignore")

    # Strict: a proxy stamps a real boolean, and a header that spells one as text is not a proxy's.
    owner: bool = Field(strict=True, description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(default=None, description="The account's user id (absent for an unshared workspace)")
    email: str | None = Field(
        default=None, description="The account's verified email, present exactly when user_id is"
    )
    agent_id: str | None = Field(default=None, description="A visiting agent's grant id; absent for a person")
    agent_name: str | None = Field(default=None, description="The name the visiting agent joined under")
    granter: str | None = Field(default=None, description="Who invited the visiting agent, as the grant records it")
    is_header_present: bool = Field(
        default=False,
        description="Whether a proxy actually stamped the request: set by the parse, never read from the wire. "
        "False tells an in-container caller (a script, the terminal) from a page's request, which both read as "
        "the owner otherwise",
    )


ANONYMOUS_OWNER: Final[RequestIdentity] = RequestIdentity(owner=True)


def parse_identity_header(header_value: str | None) -> RequestIdentity:
    """The identity a request carries; a missing or blank header reads as the anonymous owner.

    A missing header means the request came through no current proxy (an older
    forward, an in-container caller): it is treated as the owner with no account,
    never as a visitor, so ``is_header_present`` is set only for a header a proxy
    stamped. A present header that does not parse is nobody's proxy's: it raises
    ``InvalidIdentityHeaderError`` for the route to refuse, since reading it as the
    owner's would make a visitor behind a buggy proxy the owner for attribution and
    desktop ownership.
    """
    if header_value is None or not header_value.strip():
        return ANONYMOUS_OWNER
    try:
        parsed = RequestIdentity.model_validate_json(header_value)
    except ValueError as e:
        raise InvalidIdentityHeaderError(f"the {IDENTITY_HEADER} header does not parse: {e}") from e
    return RequestIdentity(
        owner=parsed.owner,
        user_id=parsed.user_id,
        email=parsed.email,
        agent_id=parsed.agent_id,
        agent_name=parsed.agent_name,
        granter=parsed.granter,
        is_header_present=True,
    )


def participant_key_of(identity: RequestIdentity) -> ParticipantKey:
    """The participant the request comes from: the account when the header names one, the visiting agent when it
    names a grant, and otherwise the owner of a workspace that has not shared."""
    if identity.user_id is not None:
        return ParticipantKey(f"user:{UserId(identity.user_id)}")
    if identity.agent_id is not None:
        return ParticipantKey(f"agent:{identity.agent_id}")
    return OWNER_PARTICIPANT_KEY


def is_owner_participant(identity: RequestIdentity) -> bool:
    """Whether the requester is the owner, under either of the owner's keys: the header says so, or it names no
    account and no grant, which ``participant_key_of`` keys ``owner`` whatever the flag says."""
    return identity.agent_id is None and (identity.owner or identity.user_id is None)


def visiting_user_id(identity: RequestIdentity) -> UserId | None:
    """The user id of a signed-in requester other than the owner; None for the owner and for a requester with no
    account."""
    if identity.owner or identity.user_id is None:
        return None
    return UserId(identity.user_id)


# Where a request with no proxy header may come from. The services bind loopback and read a header-less request as
# the owner's, since only an in-container caller (an agent's command, the shell, a script) reaches them without a
# proxy in front. But the workspace's own browser is on loopback too, and a page it shows would be read the same
# way, so a header-less request is the owner's only under a loopback ``Host``: a page that reaches loopback through
# a name of its own (a rebound or wildcard DNS name) names that host instead. And a browser marks a request made by a
# page of another origin with fetch metadata, which no in-container client sends, so such a request is refused
# whatever it carries, other than a GET or HEAD navigation (the shell framing an app's page).
LOOPBACK_ADDRESSES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1"})
LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
FETCH_SITE_HEADER: Final[str] = "Sec-Fetch-Site"
FETCH_MODE_HEADER: Final[str] = "Sec-Fetch-Mode"
_OWN_ORIGIN_FETCH_SITES: Final[frozenset[str]] = frozenset({"same-origin", "none"})
_NAVIGATE_FETCH_MODE: Final[str] = "navigate"
_NAVIGATION_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

OTHER_ORIGIN_REFUSAL: Final[str] = "only a page of this origin may call it"
UNTRUSTED_LOOPBACK_REFUSAL: Final[str] = (
    f"a request with no {IDENTITY_HEADER} header is admitted from loopback alone, under a loopback Host"
)


class RequestProvenance(FrozenModel):
    """What a request says about where it came from: the facts the provenance check reads, as the server saw them."""

    method: str = Field(description="The HTTP method")
    identity_header: str | None = Field(description="The X-Imbue-Identity header as sent, or None")
    fetch_site: str | None = Field(description="The Sec-Fetch-Site header a browser sends, or None")
    fetch_mode: str | None = Field(description="The Sec-Fetch-Mode header a browser sends, or None")
    host: str = Field(description="The Host header, with or without its port")
    remote_address: str | None = Field(description="The peer address the connection came from")


def is_loopback_address(remote_address: str | None) -> bool:
    return remote_address in LOOPBACK_ADDRESSES


def is_loopback_host(host: str) -> bool:
    """Whether a ``Host`` (with or without its port) names loopback by address or as ``localhost``."""
    try:
        hostname = urllib.parse.urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    return hostname in LOOPBACK_HOSTNAMES


def is_from_another_origin(method: str, fetch_site: str | None, fetch_mode: str | None) -> bool:
    """Whether the browser marks the request as made by a page of another origin, other than a GET or HEAD
    navigation. A request without fetch metadata (a client that is not a browser) is not judged here."""
    if fetch_site is None or fetch_site in _OWN_ORIGIN_FETCH_SITES:
        return False
    return not (fetch_mode == _NAVIGATE_FETCH_MODE and method in _NAVIGATION_METHODS)


def describe_provenance_refusal(provenance: RequestProvenance) -> str | None:
    """Why the request may not be read at all, or None when it may: a page of another origin is refused whatever it
    carries; a request a proxy stamped is then read by its header; one with no header is admitted from loopback
    alone, under a loopback ``Host``."""
    if is_from_another_origin(provenance.method, provenance.fetch_site, provenance.fetch_mode):
        return OTHER_ORIGIN_REFUSAL
    if provenance.identity_header is not None and provenance.identity_header.strip():
        return None
    if is_loopback_address(provenance.remote_address) and is_loopback_host(provenance.host):
        return None
    return UNTRUSTED_LOOPBACK_REFUSAL
