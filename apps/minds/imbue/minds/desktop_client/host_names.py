"""Host-name derivation for the workspace create flow.

Minds derives a short, pretty host-name slug from the user's arbitrary
display name, and falls back to an automatic ``workspace-N`` name when the
user leaves the create form's "Name" field empty.
"""

import re
from collections.abc import Collection
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName

# Base for auto-generated workspace host names. The generic default is never
# used bare -- it is always numbered (``workspace-1``, ``workspace-2``, ...).
_DEFAULT_HOST_NAME_BASE: Final[str] = "workspace"

# The slug target is well under mngr's ``HostName`` hard cap.
_MINDS_HOST_NAME_SLUG_MAX_LENGTH: Final[int] = 32
_NON_SLUG_CHARS_RE: Final = re.compile(r"[^a-z0-9]+")


def normalize_host_name_slug(text: str) -> HostName:
    """Convert an arbitrary human-readable name into a normalized host-name slug.

    Lowercases, replaces every run of non-alphanumeric characters with a single
    dash, trims leading/trailing dashes, and truncates to the slug length cap.
    Raises ``InvalidName`` if the result is empty (e.g. the input was all
    emoji/punctuation).
    """
    lowered = text.strip().lower()
    collapsed = _NON_SLUG_CHARS_RE.sub("-", lowered).strip("-")
    truncated = collapsed[:_MINDS_HOST_NAME_SLUG_MAX_LENGTH].strip("-")
    if not truncated:
        raise InvalidName("Workspace name must include at least one letter or number.")
    return HostName(truncated)


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


def resolve_create_host_name(submitted_host_name: str, existing_host_names: Collection[str] = ()) -> HostName:
    """Resolve the host name for a new workspace.

    The name defaults to an automatic ``workspace-N`` unless the operator types
    one into the create form's "Name" field. Resolution order:

    1. the user-submitted name, if any, normalized into a host-name slug
       (lowercased, non-alphanumeric runs collapsed to dashes, truncated);
    2. the next free ``workspace-N`` name (smallest positive ``N`` whose
       ``workspace-N`` is not already in ``existing_host_names``).

    The normalized submitted name is never uniquified -- an explicit collision
    is the API's 409 to reject, not ours to silently rename (a duplicate name
    fails the ``mngr create`` pre-flight). Only the generated ``workspace-N``
    fallback consults ``existing_host_names`` to pick a free name.

    Raises ``InvalidName`` if a non-empty submitted name normalizes to an empty
    slug (e.g. all punctuation/emoji); the generated fallback is always valid.
    """
    if submitted_host_name:
        return normalize_host_name_slug(submitted_host_name)
    return make_unique_host_name(_DEFAULT_HOST_NAME_BASE, existing_host_names, always_number=True)
