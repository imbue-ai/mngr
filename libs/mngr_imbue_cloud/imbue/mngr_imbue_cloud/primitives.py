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

# The OVH datacenter codes for those US regions, as used by the OVH order/catalog and
# ``/dedicated/server/datacenter/availabilities`` APIs and stored in ``bare_metal_servers.region``:
# ``vin`` = Vint Hill (US-EAST-VA), ``hil`` = Hillsboro (US-WEST-OR).
OVH_US_DATACENTER_CODES: Final[frozenset[str]] = frozenset({"vin", "hil"})

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


class BareMetalServerDbId(NonEmptyStr):
    """Database id of a bare_metal_servers row (server-side UUID)."""


# Wire / DB values for pool_hosts.backend_kind. Kept lowercase to match the
# connector's existing lowercase column conventions (status = available/leased/...).
BACKEND_KIND_OVH_VPS: Final[str] = "ovh_vps"
BACKEND_KIND_SLICE: Final[str] = "slice"
_BACKEND_KINDS: Final[frozenset[str]] = frozenset({BACKEND_KIND_OVH_VPS, BACKEND_KIND_SLICE})


class InvalidBackendKind(ValueError):
    """Raised when a pool-host backend_kind is not a recognized value."""


class BackendKind(NonEmptyStr):
    """How a pool host's underlying machine is provided: 'ovh_vps' or 'slice'."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if normalized not in _BACKEND_KINDS:
            raise InvalidBackendKind(f"backend_kind must be one of {sorted(_BACKEND_KINDS)}, got '{value}'")
        return super().__new__(cls, normalized)


# Wire / DB values for bare_metal_servers.status, in lifecycle order. The box
# advances ORDERED -> DELIVERED -> INSTALLING -> READY (or -> FAILED from any
# non-terminal state); the admin command moves it forward one step per run.
SERVER_STATUS_ORDERED: Final[str] = "ordered"
SERVER_STATUS_DELIVERED: Final[str] = "delivered"
SERVER_STATUS_INSTALLING: Final[str] = "installing"
SERVER_STATUS_READY: Final[str] = "ready"
SERVER_STATUS_FAILED: Final[str] = "failed"
_SERVER_STATUSES: Final[frozenset[str]] = frozenset(
    {
        SERVER_STATUS_ORDERED,
        SERVER_STATUS_DELIVERED,
        SERVER_STATUS_INSTALLING,
        SERVER_STATUS_READY,
        SERVER_STATUS_FAILED,
    }
)


class InvalidBareMetalServerStatus(ValueError):
    """Raised when a bare-metal server status is not a recognized value."""


class BareMetalServerStatus(NonEmptyStr):
    """Lifecycle state of a bare-metal server: ordered/delivered/installing/ready/failed."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if normalized not in _SERVER_STATUSES:
            raise InvalidBareMetalServerStatus(
                f"server status must be one of {sorted(_SERVER_STATUSES)}, got '{value}'"
            )
        return super().__new__(cls, normalized)


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


# Docker ``--start-arg`` flags that the pre-baked pool-host container is already
# created with -- these are the ``docker run`` flags the ``pool_host`` create
# template applies at bake time (see forever-claude-template's
# ``.mngr/settings.toml``). On the fast (adopt) path the container is reused
# as-is, so a create that requests any of these is asking for state the running
# container already has: harmless and consistent rather than a conflict. This is
# what lets the fast and slow paths accept the same start args -- the slow path
# applies them on rebuild, the fast path finds them already in effect. Any start
# arg outside this set cannot be honored by an adopted container, so the fast
# path still rejects it (use ``fast_mode=prevent`` to rebuild with it instead).
FAST_PATH_ADOPTABLE_START_ARGS: Final[frozenset[str]] = frozenset(
    {
        "--security-opt=no-new-privileges",
        "--workdir=/",
        "--restart=unless-stopped",
    }
)


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
