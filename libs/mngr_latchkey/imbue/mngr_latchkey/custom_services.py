"""User-created custom services: their naming rule, and the shape of their config entries.

A *custom service* is one an agent asked for at runtime, rather than one this
package ships. It exists as a single entry in latchkey's own ``config.json``, under
``registeredServices``, keyed by a name this module derives from the domain.
There is no second file: everything else about such a service -- its label, the
Detent scope it exposes, the domain that scope pins -- follows from that entry,
so a parallel store could only ever be a copy free to disagree with the file
that actually drives the gateway.

That makes this module pure derivation, in two directions that never meet:

* **Creating one.** :func:`custom_service_name` and
  :func:`build_custom_service_registration` turn a validated domain (plus an
  optional browser sign-in, described as ``latchkey services register`` takes
  it) into the name and the ``registeredServices`` value the desktop merges
  into ``config.json``.
* **Reading them back.** :func:`custom_service_catalog_payload` projects a
  ``registeredServices`` block into catalog entries in the shape
  ``services.json`` already uses, which is what
  :class:`~imbue.mngr_latchkey.services_catalog.ServicesCatalog` overlays and
  what ``core`` materializes for the gateway extensions.

Both directions take and return data, never a path: loading that block off disk
and writing it back both belong to ``core``.

The name is *never* parsed back into a domain. The domain is recovered from the
entry's ``baseApiUrl`` instead -- parsing a URL, which is what URLs are for --
so the naming rule only has to be injective, not reversible.

Bundled additional services (:mod:`imbue.mngr_latchkey.additional_services`,
currently just ``claude-ai``) are a separate mechanism and are untouched by
this one. They carry curated labels, brand marks and hand-written permission
schemas that a derived service has no way to express, and hosts in the field
hold rules naming their scopes, so they stay where they are. The two only meet
in the catalog, where this module's entries are overlaid on top of theirs.
"""

import re
from collections.abc import Mapping
from enum import StrEnum
from enum import auto
from typing import Final
from typing import assert_never
from urllib.parse import urlsplit

from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.baseline_permissions import GATEWAY_SELF_HOST

# Prefix marking a registration as one created here on an agent's behalf.
#
# It does two jobs. It keeps derived names clear of every service latchkey
# ships -- all of which are plain names like ``slack`` or ``google-gmail``,
# none carrying an underscore -- and of anything a user registered themselves
# with ``latchkey services register``. And it is how the catalog projection
# below recognizes its own entries, since latchkey's ``registeredServices``
# block is a flat namespace shared with those other two kinds.
#
# Latchkey's loader silently *skips* a registration whose name a builtin
# already holds, so were latchkey ever to ship a ``custom_``-prefixed service
# it would disable one of ours without erroring. ``core`` reports that case
# rather than leaving it silently dead.
CUSTOM_SERVICE_NAME_PREFIX: Final[str] = "custom_"

# What a domain may look like: dot-separated labels of ASCII letters, digits
# and hyphens. That is the alphabet a latchkey service name can carry once
# ``.`` is swapped for ``_`` (the name alphabet is ``[a-z0-9_-]``), and the
# swap is one-to-one only because ``_`` itself is excluded here. Nothing about
# public DNS is assumed beyond that: a single label, a private suffix, an IPv4
# address are all fine, since the dialog shows the user the exact origin they
# are approving. Non-ASCII names must arrive as their punycode form. Mirrored
# by ``CUSTOM_SERVICE_DOMAIN_PATTERN`` in the gateway extension.
DOMAIN_PATTERN: Final[str] = r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*$"
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(DOMAIN_PATTERN)

# The DNS limit on a full name; longer is not a hostname anything will resolve.
MAX_DOMAIN_LENGTH: Final[int] = 253


class LoginFlow(StrEnum):
    """Latchkey's generic browser sign-in flows, named as ``latchkey services register --login-flow`` names them.

    Explicit values rather than derived ones: these are latchkey's own names
    and travel verbatim into ``config.json``.
    """

    COOKIE_CAPTURE = "cookie-capture"
    TOKEN_CAPTURE = "token-capture"


class CookieCaptureParams(FrozenModel):
    """The ``--login-flow-params`` of ``cookie-capture``, keyed as latchkey keys them.

    Deserialized to reject anything latchkey would not understand, then the
    agent's original object is what gets registered.
    """

    cookie_keys: tuple[str, ...] = Field(alias="cookieKeys", description="Cookies to capture; at least one.")
    cookie_url: str | None = Field(
        default=None,
        alias="cookieUrl",
        description="URL the cookies must apply to; defaults to the login URL.",
    )


class TokenCaptureParams(FrozenModel):
    """The ``--login-flow-params`` of ``token-capture``, keyed as latchkey keys them."""

    token_url: str = Field(alias="tokenUrl", description="Endpoint whose JSON response carries the token.")
    token_field: str = Field(alias="tokenField", description="Where the token sits in that JSON, dotted if nested.")
    header: str | None = Field(
        default=None,
        description="Header to store the token as; must contain '{token}'. Defaults to a bearer Authorization header.",
    )


TOKEN_PLACEHOLDER: Final[str] = "{token}"
# What latchkey requires of ``header``: a header name, a colon, and the
# placeholder somewhere after it (``Authorization: Bearer {token}``).
_HEADER_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"^[^:\s]+:")


class Scheme(LowerCaseStrEnum):
    """The schemes a custom service may be reached over.

    The request has to name one; ``http`` exists for services on private
    networks that have no certificate, and the dialog says out loud that
    credentials sent to one travel in the clear. Detent matches a scope on
    ``protocol`` as well as ``domain``, so the scheme is pinned by the grant,
    and the scheme is part of the service name, so an http and an https
    service on one domain are two services.
    """

    HTTPS = auto()
    HTTP = auto()


SCHEMES: Final[frozenset[str]] = frozenset(member.value for member in Scheme)


class CustomServiceError(ValueError):
    """Raised when a proposed custom service is not one we can register.

    A ``ValueError`` subclass (not a ``LatchkeyError``) so this module stays
    import-light: it is read by the catalog, which must not depend on ``core``.
    """


def validate_domain(raw_domain: str) -> str:
    """Return ``raw_domain`` lower-cased, or raise :class:`CustomServiceError`.

    Lower-casing is the one repair, since domains are case-insensitive and
    everything downstream -- the name, the scope's ``const``, the
    ``baseApiUrl`` -- has to agree on one spelling. Everything else is refused
    rather than fixed, stray whitespace included: the shape :data:`DOMAIN_PATTERN` describes, the DNS
    length limit, and the gateway's own address, which a custom service must
    never be because it would have credentials injected into requests to the
    gateway's own extension.
    """
    domain = raw_domain.lower()
    if not domain:
        raise CustomServiceError("A domain is required.")
    if len(domain) > MAX_DOMAIN_LENGTH:
        raise CustomServiceError(f"Domain is longer than {MAX_DOMAIN_LENGTH} characters: {raw_domain!r}.")
    if not _DOMAIN_RE.match(domain):
        raise CustomServiceError(
            f"Domain {raw_domain!r} is not a bare hostname: expected dot-separated labels of ASCII letters, "
            "digits and hyphens (no scheme, port, path, underscore or wildcard; non-ASCII names in punycode)."
        )
    if domain == GATEWAY_SELF_HOST:
        raise CustomServiceError(f"Domain {raw_domain!r} is the latchkey gateway's own address.")
    return domain


class DomainWarning(LowerCaseStrEnum):
    """Why a valid domain still deserves a second look before it is approved.

    None of these is a refusal: a private network calls its services what it
    likes, and the dialog shows exactly which origin is being approved. They
    are the names an agent is most likely to ask for by mistake, or that could
    reach something other than what the user pictures, so the dialog says so.
    """

    # Reserved names nothing answers to (RFC 2606/6761), and Tor's ``.onion``,
    # which the gateway has no proxy for.
    UNREACHABLE = auto()
    # Resolved by the workspace machine's own network -- loopback, mDNS,
    # private-use suffixes, a single label -- so it may reach a different host
    # on each machine.
    LOCAL_NAME = auto()
    # An address rather than a name: whatever machine holds it gets the
    # credentials.
    IP_ADDRESS = auto()
    # A punycode label, which can render as a look-alike of another site.
    LOOKALIKE = auto()


_UNREACHABLE_SUFFIXES: Final[frozenset[str]] = frozenset({"invalid", "test", "example", "onion"})
_UNREACHABLE_DOMAINS: Final[tuple[str, ...]] = ("example.com", "example.net", "example.org")
_LOCAL_SUFFIXES: Final[frozenset[str]] = frozenset({"localhost", "local", "internal", "lan", "corp", "home"})
_PUNYCODE_LABEL_PREFIX: Final[str] = "xn--"


def domain_warning(domain: str) -> DomainWarning | None:
    """Return why ``domain`` (already valid) deserves a warning in the dialog, or ``None``.

    Checked from most to least certain: a reserved name is a mistake whatever
    else it looks like, a punycode label is worth flagging even on a public
    suffix, and an all-numeric last label is an address rather than a name
    (no top-level domain is numeric), before the local-name shapes.
    """
    labels = domain.split(".")
    if labels[-1] in _UNREACHABLE_SUFFIXES or any(
        domain == reserved or domain.endswith(f".{reserved}") for reserved in _UNREACHABLE_DOMAINS
    ):
        return DomainWarning.UNREACHABLE
    if any(label.startswith(_PUNYCODE_LABEL_PREFIX) for label in labels):
        return DomainWarning.LOOKALIKE
    if labels[-1].isdigit():
        return DomainWarning.IP_ADDRESS
    if len(labels) == 1 or labels[-1] in _LOCAL_SUFFIXES or domain.endswith(".home.arpa"):
        return DomainWarning.LOCAL_NAME
    return None


def custom_service_name(domain: str, scheme: str) -> str:
    """Return the latchkey service name for ``scheme`` and ``domain``.

    ``https`` + ``example.com`` -> ``custom_https_example_com``. Latchkey's name
    alphabet (``^[a-z0-9][a-z0-9_-]*$``) admits no ``.``, so some substitution is
    forced; it does admit ``_``, and :func:`validate_domain` rejects a domain
    containing one, so ``.`` -> ``_`` is one-to-one. The scheme goes in front,
    followed by ``_``: the two schemes are fixed strings neither of which is a
    prefix of the other plus ``_``, so the pair is recoverable and two
    services -- one per scheme -- can share a domain. Two (scheme, domain)
    pairs can never produce one name, which the obvious ``.`` -> ``-`` would
    not give us, since it maps both ``a-b.com`` and ``a.b.com`` to ``a-b-com``.

    Reversibility is deliberately *not* provided: nothing needs to turn a name
    back into a domain (see the module docstring), so only injectivity is
    promised and only injectivity is tested.
    """
    return f"{CUSTOM_SERVICE_NAME_PREFIX}{scheme}_{domain.replace('.', '_')}"


def is_custom_service_name(name: str) -> bool:
    """Whether ``name`` is one derived here, rather than a builtin or a user's own registration."""
    return name.startswith(CUSTOM_SERVICE_NAME_PREFIX)


def custom_service_label(domain: str, scheme: str) -> str:
    """Return the label a custom service carries everywhere it is named: its origin, ``https://example.com``.

    The origin rather than the bare domain, because an http and an https
    service on one domain are two services and have to be told apart in a
    list; and rather than anything the agent could supply, because the origin
    is the one string that cannot misdescribe what the connection reaches.
    """
    return f"{scheme}://{domain}"


def base_api_url_for_domain(domain: str, scheme: str) -> str:
    """Return the ``baseApiUrl`` a custom service registers for ``domain``.

    Always the bare origin. Latchkey selects a service by ``url.startsWith(baseApiUrl)``
    while detent matches the scope on ``domain``, which it derives as the URL's
    *hostname* -- so an origin is the only base URL for which the two describe
    the same set of requests. A port would narrow latchkey's matching while
    staying invisible to detent, and a path would inject credentials more
    narrowly than the scope grants.
    """
    return f"{scheme}://{domain}/"


def validate_scheme(raw_scheme: str) -> Scheme:
    """Return the scheme, or raise :class:`CustomServiceError` if it is not one a custom service may use."""
    scheme = raw_scheme.strip().lower()
    if scheme not in SCHEMES:
        raise CustomServiceError(f"scheme must be one of {sorted(SCHEMES)}, got {raw_scheme!r}")
    return Scheme(scheme)


def scheme_from_base_api_url(base_api_url: str) -> Scheme | None:
    """Return the scheme of ``base_api_url`` when it is one a custom service may use, else ``None``."""
    try:
        scheme = urlsplit(base_api_url).scheme.lower()
    except ValueError:
        return None
    return Scheme(scheme) if scheme in SCHEMES else None


def domain_from_base_api_url(base_api_url: str) -> str | None:
    """Return the hostname of ``base_api_url``, or ``None`` when it has none.

    The hostname specifically, because that is what detent compares against the
    scope's ``const``: a hand-edited entry that grew a port or a path still
    resolves to the domain the permission check will actually see.
    """
    try:
        hostname = urlsplit(base_api_url).hostname
    except ValueError:
        return None
    if hostname is None or not hostname:
        return None
    return hostname.lower()


def build_custom_service_scope_schema(domain: str, scheme: str) -> dict[str, JsonValue]:
    """Return the Detent scope schema pinning a request to ``scheme`` and ``domain``.

    Both, so the grant covers exactly the origin the registration names and the
    dialog showed: detent decomposes a request into ``protocol`` and ``domain``
    among other things, and a rule on the domain alone would also admit the
    other scheme.

    The same shape the bundled additional services use. It is emitted both into
    the catalog projection and, by the gateway extension, into the approval
    effect -- the grant carries its own scope schema so the permissions file it
    lands in is self-contained.
    """
    return {
        "properties": {"domain": {"const": domain}, "protocol": {"const": scheme}},
        "required": ["domain", "protocol"],
    }


def _is_within_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _validate_url_on_domain(raw_url: str, field: str, domain: str) -> None:
    """Require ``raw_url`` to be an http(s) URL on ``domain`` or a subdomain of it.

    The scheme is the user's business -- the dialog names where the browser
    will go -- so either is accepted; the domain check is what keeps a sign-in,
    and the credentials it captures, on the service being approved.
    """
    if not raw_url:
        raise CustomServiceError(f"{field} is required and must be a non-empty string.")
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        raise CustomServiceError(f"{field} is not a valid URL: {raw_url!r}.") from None
    if parsed.scheme.lower() not in SCHEMES or parsed.hostname is None:
        raise CustomServiceError(f"{field} must be an http or https URL. Got {raw_url!r}.")
    if not _is_within_domain(parsed.hostname.lower(), domain):
        raise CustomServiceError(f"{field} must be on {domain} or a subdomain of it. Got {raw_url!r}.")


def _validate_cookie_capture_params(params: CookieCaptureParams, domain: str) -> None:
    if not params.cookie_keys or any(not key for key in params.cookie_keys):
        raise CustomServiceError("login_flow_params.cookieKeys must name at least one cookie, none empty.")
    if params.cookie_url is not None:
        _validate_url_on_domain(params.cookie_url, "login_flow_params.cookieUrl", domain)


def _validate_token_capture_params(params: TokenCaptureParams, domain: str) -> None:
    _validate_url_on_domain(params.token_url, "login_flow_params.tokenUrl", domain)
    if not params.token_field:
        raise CustomServiceError("login_flow_params.tokenField must be a non-empty string.")
    if params.header is not None and (
        TOKEN_PLACEHOLDER not in params.header or not _HEADER_SHAPE_RE.match(params.header)
    ):
        raise CustomServiceError(
            f"login_flow_params.header must be a header line containing {TOKEN_PLACEHOLDER!r}, "
            f"such as 'Authorization: Bearer {TOKEN_PLACEHOLDER}'."
        )


def validate_login_flow(
    domain: str,
    login_url: str | None,
    login_flow: LoginFlow | None,
    login_flow_params: Mapping[str, JsonValue] | None,
) -> None:
    """Raise :class:`CustomServiceError` unless the three login fields describe one browser sign-in, or none.

    The fields mirror ``latchkey services register``'s ``--login-url``,
    ``--login-flow`` and ``--login-flow-params``, and so do the rules: a flow
    needs a login URL, and each flow has parameters of its own. The parameters
    are deserialized against latchkey's schema for the flow -- an unknown key
    is a mistake latchkey would carry silently -- and every URL among them has
    to be on ``domain``, since that is where the captured credentials go. What
    passes is registered as the agent sent it.
    """
    if login_url is None and login_flow is None and login_flow_params is None:
        return
    if login_flow is None:
        raise CustomServiceError("login_flow is required when login_url or login_flow_params is given.")
    if login_url is None:
        raise CustomServiceError(f"login_url is required for the {login_flow.value} login flow.")
    if login_flow_params is None:
        raise CustomServiceError(f"login_flow_params is required for the {login_flow.value} login flow.")
    _validate_url_on_domain(login_url, "login_url", domain)
    try:
        match login_flow:
            case LoginFlow.COOKIE_CAPTURE:
                _validate_cookie_capture_params(CookieCaptureParams.model_validate(login_flow_params), domain)
            case LoginFlow.TOKEN_CAPTURE:
                _validate_token_capture_params(TokenCaptureParams.model_validate(login_flow_params), domain)
            case unreachable:
                assert_never(unreachable)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'params'}: {error['msg']}" for error in e.errors()
        )
        raise CustomServiceError(f"login_flow_params is not valid for {login_flow.value}: {problems}") from None


def build_custom_service_registration(
    domain: str,
    scheme: str,
    login_url: str | None = None,
    login_flow: LoginFlow | None = None,
    login_flow_params: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Return the ``registeredServices`` value for a custom service on ``domain``.

    Written in latchkey's own shape, so it lands verbatim under
    ``registeredServices.<name>`` exactly as ``latchkey services register``
    would persist it -- the login fields are its ``--login-url``,
    ``--login-flow`` and ``--login-flow-params``, already checked by
    :func:`validate_login_flow` and passed through as given.

    With no login the service is registered with a ``baseApiUrl`` alone, which
    latchkey reports as connectable via ``latchkey auth set`` -- the
    manual-credential path the permission dialog already renders as a form.
    """
    registration: dict[str, JsonValue] = {"baseApiUrl": base_api_url_for_domain(domain, scheme)}
    if login_url is None or login_flow is None or login_flow_params is None:
        return registration
    registration["loginUrl"] = login_url
    registration["loginFlow"] = {"name": login_flow.value, "params": dict(login_flow_params)}
    return registration


def registration_login_url(registration: Mapping[str, JsonValue]) -> str | None:
    """Return the browser sign-in URL of a registration, or ``None`` if it has no sign-in.

    The inverse of the ``loginUrl`` :func:`build_custom_service_registration`
    writes, read back for a service that already exists: a second request for
    its origin is shown, and signed in through, the registration this computer
    actually has rather than the login the agent guessed.
    """
    login_url = registration.get("loginUrl")
    return login_url if isinstance(login_url, str) else None


def custom_service_catalog_payload(
    registered_services: Mapping[str, JsonValue],
    shipped_service_names: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, object]]]:
    """Project the ``custom_``-prefixed registrations into ``services.json``-shaped entries.

    The result matches what
    :func:`imbue.mngr_latchkey.services_catalog.service_infos_from_catalog_payload`
    expects, so the catalog can overlay these on the shipped file without either
    side knowing where the other came from. Each service exposes exactly one
    scope, so its value is a single-element list, and it lists no permissions:
    the grant uses Detent's builtin catch-all ``any``, which every scope
    implicitly admits, so there is no permission schema to define.

    ``shipped_service_names`` are the names this build already serves; a custom
    entry may never override one, so the overlay only ever *adds*.

    ``config.json`` is hand-editable and shared with latchkey itself, so an
    entry we cannot make sense of is skipped with a warning rather than raising:
    one malformed registration must not take the whole catalog -- and with it
    every *other* service's permission dialog -- down.
    """
    payload: dict[str, list[dict[str, object]]] = {}
    for name, value in registered_services.items():
        if not is_custom_service_name(name):
            continue
        if name in shipped_service_names:
            # The ``custom_`` prefix is meant to keep our names clear of
            # latchkey's, but nothing upstream promises that. If a release ever
            # ships one, latchkey's loader silently skips *our* registration in
            # favour of the builtin -- so say so here rather than let the
            # service quietly stop working.
            logger.warning(
                "Custom service {} is shadowed by a service this build ships; it will not work "
                "until it is recreated under another domain",
                name,
            )
            continue
        if not isinstance(value, dict):
            logger.warning("Ignoring custom service {}: its registration is not a JSON object", name)
            continue
        base_api_url = value.get("baseApiUrl")
        if not isinstance(base_api_url, str):
            logger.warning("Ignoring custom service {}: its registration has no string baseApiUrl", name)
            continue
        domain = domain_from_base_api_url(base_api_url)
        if domain is None:
            logger.warning("Ignoring custom service {}: baseApiUrl {!r} has no host", name, base_api_url)
            continue
        scheme = scheme_from_base_api_url(base_api_url)
        if scheme is None:
            logger.warning("Ignoring custom service {}: baseApiUrl {!r} has no usable scheme", name, base_api_url)
            continue
        payload[name] = [
            {
                "scope": name,
                "display_name": custom_service_label(domain, scheme),
                "description": f"Requests to {base_api_url_for_domain(domain, scheme)}.",
                "permissions": [],
                # The scope schema travels with the entry because a custom
                # scope is not a detent builtin: any grant referencing it has
                # to carry its definition, and this is the only place both the
                # desktop and the gateway can read it from.
                "scope_schema": build_custom_service_scope_schema(domain, scheme),
            }
        ]
    return payload
