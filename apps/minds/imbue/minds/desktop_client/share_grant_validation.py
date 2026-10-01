"""The rules deciding which entries a workspace's grants document may carry.

The same rules run in two places: on the grants route, over the whole document
a save carries, and in the share panel, before a typed entry becomes a row. The
panel gets :data:`PUBLIC_EMAIL_DOMAINS` with its workspace options payload so
the two can never disagree about which domains are public.

Addresses and domains are compared, and stored, trimmed and lowercased (a
domain also loses a leading ``@``), which is the form the in-workspace gateway
compares a visitor's address against. A refusal names that normalized form in
its message and echoes the raw entry in its ``value``, so the panel can mark the
row the granter actually typed.

An internationalized label is accepted as it is written and stored lowercased in
Unicode, never as punycode: the gateway lowercases both sides of the comparison
and never decodes, so a punycode entry would match nobody.
"""

import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.api_models import SharingGrantList
from imbue.minds.desktop_client.api_models import SharingGrantsDocument

# Mail providers anyone can sign up at: granting one of these as a domain would
# admit the whole internet, so only individual addresses at them can be granted.
PUBLIC_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.ca",
        "yahoo.co.id",
        "yahoo.co.in",
        "yahoo.co.jp",
        "yahoo.co.nz",
        "yahoo.co.uk",
        "yahoo.com.ar",
        "yahoo.com.au",
        "yahoo.com.br",
        "yahoo.com.hk",
        "yahoo.com.mx",
        "yahoo.com.ph",
        "yahoo.com.sg",
        "yahoo.com.tw",
        "yahoo.com.vn",
        "yahoo.de",
        "yahoo.dk",
        "yahoo.es",
        "yahoo.fr",
        "yahoo.gr",
        "yahoo.ie",
        "yahoo.it",
        "yahoo.nl",
        "yahoo.no",
        "yahoo.pl",
        "yahoo.se",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
        "hey.com",
        "fastmail.com",
        "tuta.io",
    }
)

# The scope a refusal names for the workspace-level lists; every other scope is
# named by the app whose list carried the entry.
WORKSPACE_GRANT_SCOPE: Final[str] = "workspace"

# The kind of entry a refusal names. A granter types an address or a domain; a
# user id is the gateway's own upgrade of an email grant, so it is opaque.
EMAIL_GRANT_KIND: Final[str] = "email"
EMAIL_DOMAIN_GRANT_KIND: Final[str] = "email_domain"
USER_GRANT_KIND: Final[str] = "user"

# Deliberately simpler than RFC 5321, because the panel runs the same rules in
# TypeScript. ``[^\W_]`` is "letter or digit" in any script (Python's \w minus
# the underscore it includes), so an internationalized label passes as written.
_DOMAIN_LABEL: Final[str] = r"[^\W_](?:[^\W_]|-)*(?<!-)"
# Labels carry inner hyphens; the last one is letters only, at least two.
_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\.[^\W\d_]{{2,}}$")
_EMAIL_LOCAL_PART_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+$")


class GrantRefusal(FrozenModel):
    """One entry a grants document may not carry, and the reason the panel shows."""

    scope: str = Field(description="'workspace', or the name of the app whose list carried the entry")
    kind: str = Field(description="'email' or 'email_domain'")
    value: str = Field(description="The entry exactly as the caller sent it")
    message: str = Field(description="Why the entry was refused")


@pure
def normalize_grant_address(value: str) -> str:
    """The form an address is compared by, and stored as."""
    return value.strip().lower()


@pure
def normalize_grant_user_id(value: str) -> str:
    """The form a user id is compared by, and stored as (opaque, so never case-folded)."""
    return value.strip()


@pure
def normalize_grant_domain(value: str) -> str:
    """The form a domain is compared by, and stored as (``@example.com`` is the same grant as ``example.com``)."""
    return normalize_grant_address(value).removeprefix("@")


@pure
def is_email_domain(value: str) -> bool:
    return _DOMAIN_PATTERN.match(normalize_grant_domain(value)) is not None


@pure
def is_email_address(value: str) -> bool:
    local_part, separator, domain = normalize_grant_address(value).partition("@")
    return bool(separator) and _EMAIL_LOCAL_PART_PATTERN.match(local_part) is not None and is_email_domain(domain)


@pure
def normalized_grants_document(document: SharingGrantsDocument) -> SharingGrantsDocument:
    """``document`` in the form it is stored in, so the file holds what was checked."""
    return SharingGrantsDocument(
        workspace=_normalized_grant_list(document.workspace),
        services={name: _normalized_grant_list(entry) for name, entry in document.services.items()},
    )


@pure
def _normalized_grant_list(grants: SharingGrantList) -> SharingGrantList:
    return SharingGrantList(
        users=tuple(normalize_grant_user_id(value) for value in grants.users),
        emails=tuple(normalize_grant_address(value) for value in grants.emails),
        email_domains=tuple(normalize_grant_domain(value) for value in grants.email_domains),
    )


@pure
def validate_grants_document(
    document: SharingGrantsDocument,
    granter_email: str,
    # What the workspace already grants: an entry in force is never re-refused,
    # so only what this save introduces is checked.
    stored_document: SharingGrantsDocument,
) -> tuple[GrantRefusal, ...]:
    """Every entry ``document`` adds that must not be granted, workspace scope first then each app's.

    An entry is checked against the list it sits in before its own value is, so
    a second copy in one list is a duplicate whatever it holds.
    """
    refusals = _refuse_new_entries(document.workspace, stored_document.workspace, WORKSPACE_GRANT_SCOPE, granter_email)
    for service_name in sorted(document.services):
        stored_list = stored_document.services.get(service_name, SharingGrantList())
        refusals.extend(_refuse_new_entries(document.services[service_name], stored_list, service_name, granter_email))
    return tuple(refusals)


@pure
def _refuse_new_entries(
    grants: SharingGrantList, stored_grants: SharingGrantList, scope: str, granter_email: str
) -> list[GrantRefusal]:
    stored_user_ids = frozenset(normalize_grant_user_id(value) for value in stored_grants.users)
    stored_addresses = frozenset(normalize_grant_address(value) for value in stored_grants.emails)
    stored_domains = frozenset(normalize_grant_domain(value) for value in stored_grants.email_domains)
    return [
        *_refuse_user_entries(grants.users, stored_user_ids, scope),
        *_refuse_email_entries(grants.emails, stored_addresses, scope, granter_email),
        *_refuse_email_domain_entries(grants.email_domains, stored_domains, scope),
    ]


@pure
def _refuse_user_entries(values: Sequence[str], stored: AbstractSet[str], scope: str) -> list[GrantRefusal]:
    already_granted: set[str] = set()
    refusals: list[GrantRefusal] = []
    for value in values:
        normalized = normalize_grant_user_id(value)
        message = _user_refusal_message(normalized, already_granted, stored)
        if message is not None:
            refusals.append(GrantRefusal(scope=scope, kind=USER_GRANT_KIND, value=value, message=message))
        already_granted.add(normalized)
    return refusals


@pure
def _user_refusal_message(normalized: str, already_granted: AbstractSet[str], stored: AbstractSet[str]) -> str | None:
    # Emptiness is judged before duplication: a blank is not an entry at all, so
    # calling a second blank a duplicate of the first would name the wrong thing.
    if normalized in stored:
        return None
    if not normalized:
        return f"{normalized} is not a user id."
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    return None


@pure
def _refuse_email_entries(
    values: Sequence[str], stored: AbstractSet[str], scope: str, granter_email: str
) -> list[GrantRefusal]:
    granter = normalize_grant_address(granter_email)
    already_granted: set[str] = set()
    refusals: list[GrantRefusal] = []
    for value in values:
        normalized = normalize_grant_address(value)
        message = _email_refusal_message(normalized, granter, already_granted, stored)
        if message is not None:
            refusals.append(GrantRefusal(scope=scope, kind=EMAIL_GRANT_KIND, value=value, message=message))
        already_granted.add(normalized)
    return refusals


@pure
def _email_refusal_message(
    normalized: str, granter: str, already_granted: AbstractSet[str], stored: AbstractSet[str]
) -> str | None:
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    if normalized in stored:
        return None
    if not is_email_address(normalized):
        return f"{normalized} is not an email address."
    if normalized == granter:
        return f"{normalized} is your own address."
    return None


@pure
def _refuse_email_domain_entries(values: Sequence[str], stored: AbstractSet[str], scope: str) -> list[GrantRefusal]:
    already_granted: set[str] = set()
    refusals: list[GrantRefusal] = []
    for value in values:
        normalized = normalize_grant_domain(value)
        message = _email_domain_refusal_message(normalized, already_granted, stored)
        if message is not None:
            refusals.append(GrantRefusal(scope=scope, kind=EMAIL_DOMAIN_GRANT_KIND, value=value, message=message))
        already_granted.add(normalized)
    return refusals


@pure
def _email_domain_refusal_message(
    normalized: str, already_granted: AbstractSet[str], stored: AbstractSet[str]
) -> str | None:
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    if normalized in stored:
        return None
    if not is_email_domain(normalized):
        return f"{normalized} is not a domain."
    if normalized in PUBLIC_EMAIL_DOMAINS:
        return f"{normalized} cannot be granted permissions because it is a public email provider."
    return None
