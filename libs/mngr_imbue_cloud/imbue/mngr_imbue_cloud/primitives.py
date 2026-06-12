import re
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr

IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

# OVH-US datacenters the imbue_cloud host pool can land VPSes in. Used to
# validate the ``region`` create-path knob client-side (the connector itself
# accepts any string and simply matches the column). Kept small and explicit on
# purpose; extend when the pool gains new datacenters.
KNOWN_OVH_US_REGIONS: Final[frozenset[str]] = frozenset({"US-EAST-VA", "US-WEST-OR"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidImbueCloudAccount(ValueError):
    """Raised when an account email fails validation."""


class ImbueCloudAccount(NonEmptyStr):
    """Email address identifying an Imbue Cloud account."""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip().lower()
        if not _EMAIL_RE.match(stripped):
            raise InvalidImbueCloudAccount(f"Not a valid email address: '{value}'")
        return super().__new__(cls, stripped)


class SuperTokensUserId(NonEmptyStr):
    """The SuperTokens user_id (UUID v4)."""


class LeaseDbId(NonEmptyStr):
    """Database id of a leased host (server-side UUID)."""


class ImbueCloudKeyType(UpperCaseStrEnum):
    """The class of secret being requested."""

    LITELLM = auto()


class FastMode(UpperCaseStrEnum):
    """Whether ``mngr create`` on imbue_cloud may take the fast (adopt) path.

    REQUIRE: only the fast path -- lease an exact attribute match and adopt
    its pre-baked agent. If no exact match exists, raise
    ``FastPathUnavailableError`` rather than falling back.

    PREVENT: only the slow path -- lease any adequately-sized available host
    (relaxed attributes), destroy its baked container, and rebuild the host
    from scratch like an OVH host. This is the default: it always works as
    long as the pool has any free host.
    """

    REQUIRE = auto()
    PREVENT = auto()


# The fast-path adopt optimization is opt-in: a bare ``mngr create`` against
# imbue_cloud does the robust full rebuild unless the caller explicitly asks
# for the fast path via ``-b fast_mode=require``.
DEFAULT_FAST_MODE: Final[FastMode] = FastMode.PREVENT


class InvalidR2BucketAccess(ValueError):
    """Raised when an R2 key access scope is not 'read' or 'readwrite'."""


_R2_ACCESS_VALUES: Final[tuple[str, ...]] = ("read", "readwrite")


class R2BucketAccess(NonEmptyStr):
    """Access scope for an R2 bucket key: 'read' or 'readwrite' (lowercase wire form)."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if normalized not in _R2_ACCESS_VALUES:
            raise InvalidR2BucketAccess(f"access must be one of {_R2_ACCESS_VALUES}, got '{value}'")
        return super().__new__(cls, normalized)


class R2BucketShortName(NonEmptyStr):
    """A user-supplied short bucket name (the connector derives the full R2 name)."""


class R2AccessKeyId(NonEmptyStr):
    """An S3 Access Key ID for an R2 bucket key (= the Cloudflare token id)."""


def slugify_account(account: str) -> str:
    """Produce a stable, filesystem-safe slug for use in provider instance names.

    Lowercases, replaces non-alphanumeric characters with hyphens, collapses
    runs of hyphens, and strips leading/trailing hyphens. Used by minds when
    writing dynamic provider instance entries.
    """
    lowered = account.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise InvalidImbueCloudAccount(f"Cannot slugify account: '{account}'")
    return slug
