"""Host-name derivation for the workspace create flow.

Imbue Studio derives a short, pretty host-name slug from the user's arbitrary
display name, and falls back to an automatic ``workspace-N`` name when the
user leaves the create form's "Name" field empty. An operator can replace the
``workspace`` base of that automatic name (``MINDS_DEFAULT_WORKSPACE_NAME_BASE``).
"""

import re
from collections.abc import Collection
from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.errors import DefaultWorkspaceNameBaseError
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import MAX_HOST_NAME_LENGTH

# Replaces the generic base of an automatic workspace name: a create with no name is then
# named the base itself, numbered only once that is taken.
DEFAULT_WORKSPACE_NAME_BASE_ENV_VAR: Final[str] = "MINDS_DEFAULT_WORKSPACE_NAME_BASE"
# Leaves a configured base room for the number a taken name gets.
_CONFIGURED_HOST_NAME_BASE_MAX_LENGTH: Final[int] = MAX_HOST_NAME_LENGTH - 4

# Base for auto-generated workspace host names. The generic default is never
# used bare -- it is always numbered (``workspace-1``, ``workspace-2``, ...).
_DEFAULT_HOST_NAME_BASE: Final[str] = "workspace"

# The slug target is well under mngr's ``HostName`` hard cap.
_MINDS_HOST_NAME_SLUG_MAX_LENGTH: Final[int] = 32
_NON_SLUG_CHARS_RE: Final = re.compile(r"[^a-z0-9]+")


@pure
def _collapse_to_slug_characters(text: str) -> str:
    """Lowercase ``text`` and replace every run of non-alphanumeric characters with a single dash."""
    return _NON_SLUG_CHARS_RE.sub("-", text.strip().lower()).strip("-")


def normalize_host_name_slug(text: str) -> HostName:
    """Convert an arbitrary human-readable name into a normalized host-name slug.

    Lowercases, replaces every run of non-alphanumeric characters with a single
    dash, trims leading/trailing dashes, and truncates to the slug length cap.
    Raises ``InvalidName`` if the result is empty (e.g. the input was all
    emoji/punctuation).
    """
    truncated = _collapse_to_slug_characters(text)[:_MINDS_HOST_NAME_SLUG_MAX_LENGTH].strip("-")
    if not truncated:
        raise InvalidName("Workspace name must include at least one letter or number.")
    return HostName(truncated)


def read_configured_host_name_base(environ: Mapping[str, str]) -> HostName | None:
    """The operator's base for automatic workspace names, or None when the variable is unset or empty.

    The base is normalized to slug characters but, unlike a typed name, never
    shortened: the name a create gets is the whole base, so a base too long to
    be one is refused. Raises ``DefaultWorkspaceNameBaseError`` naming the
    variable for a value with no letter or number, or one too long.
    """
    configured = environ.get(DEFAULT_WORKSPACE_NAME_BASE_ENV_VAR, "")
    if not configured:
        return None
    slug = _collapse_to_slug_characters(configured)
    if not slug:
        raise DefaultWorkspaceNameBaseError(
            f"{DEFAULT_WORKSPACE_NAME_BASE_ENV_VAR}={configured!r} is not a usable workspace name base: "
            "it must include at least one letter or number."
        )
    if len(slug) > _CONFIGURED_HOST_NAME_BASE_MAX_LENGTH:
        raise DefaultWorkspaceNameBaseError(
            f"{DEFAULT_WORKSPACE_NAME_BASE_ENV_VAR}={configured!r} is not a usable workspace name base: "
            f"it must be at most {_CONFIGURED_HOST_NAME_BASE_MAX_LENGTH} characters."
        )
    return HostName(slug)


@pure
def make_unique_host_name(base: str, existing_host_names: Collection[str], *, always_number: bool = False) -> HostName:
    """Return a host name derived from ``base`` that avoids ``existing_host_names``.

    ``existing_host_names`` is the set of host names already in use across every
    provider (the create handler gathers it from the discovery snapshot).

    With ``always_number`` False, ``base`` is returned as-is when free, else the
    smallest free ``base-2``, ``base-3``, ... -- a readable bare name that is
    numbered only once it collides.

    With ``always_number`` True, ``base`` is never used bare: the smallest free
    ``base-1``, ``base-2``, ... is returned. This is the generic default's
    scheme, which has no bare ``workspace`` form; a gap left by a destroyed
    ``workspace-2`` is reused before climbing to ``workspace-4``.

    Raises ``InvalidName`` if the chosen name is not a valid ``HostName`` (i.e.
    ``base`` itself is invalid); appending ``-N`` to a valid base stays valid.
    """
    existing = set(existing_host_names)
    if not always_number and base not in existing:
        return HostName(base)
    n = 1 if always_number else 2
    while f"{base}-{n}" in existing:
        n += 1
    return HostName(f"{base}-{n}")


def resolve_create_host_name(
    submitted_host_name: str,
    existing_host_names: Collection[str] = (),
    configured_host_name_base: HostName | None = None,
) -> HostName:
    """Resolve the host name for a new workspace.

    The name defaults to an automatic one unless the operator types one into
    the create form's "Name" field. Resolution order:

    1. the user-submitted name, if any, normalized into a host-name slug
       (lowercased, non-alphanumeric runs collapsed to dashes, truncated);
    2. with a configured base, the base itself when it is not already in
       ``existing_host_names``, else the smallest free ``base-N`` from 2;
    3. the next free ``workspace-N`` name (smallest positive ``N`` whose
       ``workspace-N`` is not already in ``existing_host_names``).

    The normalized submitted name is never uniquified -- an explicit collision
    is the API's 409 to reject, not ours to silently rename (a duplicate name
    fails the ``mngr create`` pre-flight). Only the generated names consult
    ``existing_host_names`` to pick a free one.

    Raises ``InvalidName`` if a non-empty submitted name normalizes to an empty
    slug (e.g. all punctuation/emoji); the generated names are always valid.
    """
    if submitted_host_name:
        return normalize_host_name_slug(submitted_host_name)
    if configured_host_name_base is not None:
        return make_unique_host_name(configured_host_name_base, existing_host_names)
    return make_unique_host_name(_DEFAULT_HOST_NAME_BASE, existing_host_names, always_number=True)
