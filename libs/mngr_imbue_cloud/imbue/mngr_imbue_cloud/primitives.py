import re
from collections.abc import Mapping
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

# The minds environment tiers. Every env name maps to exactly one tier, and every
# bare-metal box belongs to exactly one tier. Tiers are isolated by construction --
# each has its own pool-management SSH keypair, and there is meant to be zero
# cross-tier reach. Sharing a box WITHIN a tier is fine and routine (several
# ``dev-<user>`` envs on one dev box); sharing one ACROSS tiers puts both tiers'
# pool keys on the box, handing each tier's operators and connector ``limactl`` --
# and so root -- over the other's workspaces, which is what
# ``assert_box_is_exclusive_to_tier`` refuses.
PRODUCTION_TIER: Final[str] = "production"
STAGING_TIER: Final[str] = "staging"
DEV_TIER: Final[str] = "dev"
CI_TIER: Final[str] = "ci"

# Public keys a correctly prepped box authorizes for its lima service user: exactly
# the one pool-management key of the tier that owns the box.
# ``bare_metal_prep.build_box_prep_script`` writes ``authorized_keys`` with a
# single-key overwrite (``cat >``, never an append), so any other count means a key
# was added out of band.
EXPECTED_AUTHORIZED_KEY_COUNT: Final[int] = 1

# The OVH-US regions the imbue_cloud host pool can land hosts in (the lease-region
# labels stamped on pool rows), each mapped to the OVH datacenter code serving it,
# as used by the OVH order/catalog and ``/dedicated/server/datacenter/availabilities``
# APIs and stored in ``bare_metal_servers.region``: ``vin`` = Vint Hill,
# ``hil`` = Hillsboro. The single source for the pairing -- the region/datacenter
# collections below derive from it. Kept small and explicit on purpose; extend
# when the pool gains new datacenters.
OVH_DATACENTER_CODE_BY_US_REGION: Final[Mapping[str, str]] = {"US-EAST-VA": "vin", "US-WEST-OR": "hil"}

# Used to validate the ``region`` create-path knob client-side (the connector
# itself accepts any string and simply matches the column).
KNOWN_OVH_US_REGIONS: Final[frozenset[str]] = frozenset(OVH_DATACENTER_CODE_BY_US_REGION)

# The reverse pairing: the lease-region label served by each OVH datacenter code
# (as stored in ``bare_metal_servers.region``). Derived from the forward map so
# the two can never disagree.
US_REGION_BY_OVH_DATACENTER_CODE: Final[Mapping[str, str]] = {
    datacenter: region for region, datacenter in OVH_DATACENTER_CODE_BY_US_REGION.items()
}

OVH_US_DATACENTER_CODES: Final[frozenset[str]] = frozenset(OVH_DATACENTER_CODE_BY_US_REGION.values())

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


@pure
def tier_for_env_name(env_name: str) -> str:
    """The tier a minds env name belongs to.

    ``production`` and ``staging`` are each their own tier; a ``ci-`` prefix marks
    the CI orchestrator's ephemeral envs; every other name (by convention
    ``dev-<user>``) is a dev env. This is the canonical definition --
    the minds operator CLI re-exports it rather than keeping a second
    copy, so the box-exclusivity guard and the minds CLI can never disagree about
    which tier an env belongs to.
    """
    if env_name == PRODUCTION_TIER:
        return PRODUCTION_TIER
    if env_name == STAGING_TIER:
        return STAGING_TIER
    if env_name.startswith(f"{CI_TIER}-"):
        return CI_TIER
    return DEV_TIER


@pure
def is_box_exclusive_to_tier(*, authorized_key_count: int, foreign_tier_slice_count: int) -> bool:
    """Whether a bare-metal box belongs solely to the tier reading it.

    The one definition of the rule, so the bake-time guard
    (``assert_box_is_exclusive_to_tier``) and the read-only audit
    (``minds-admin server list --verify-occupancy``, which tells operators a bake would
    refuse) can never disagree. A box is exclusive when it authorizes exactly the
    owning tier's pool key and carries no slice stamped for an env in another tier.
    """
    return authorized_key_count == EXPECTED_AUTHORIZED_KEY_COUNT and foreign_tier_slice_count == 0


class SuperTokensUserId(NonEmptyStr):
    """The SuperTokens user_id (UUID v4)."""


class LeaseDbId(NonEmptyStr):
    """Database id of a leased host (server-side UUID)."""


class BareMetalServerDbId(NonEmptyStr):
    """Database id of a bare_metal_servers row (server-side UUID)."""


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


class PoolHostDestroyOutcomeStatus(LowerCaseStrEnum):
    """Per-host outcome of an operator pool destroy, as emitted in the JSON report.

    Lowercase wire values (``destroyed`` / ``skipped_leased`` / ``already_gone`` /
    ``failed``) -- the format operators and scripts read from
    ``minds-admin pool destroy`` and ``teardown-slices``.
    """

    DESTROYED = auto()
    SKIPPED_LEASED = auto()
    ALREADY_GONE = auto()
    FAILED = auto()


class SliceBakeOutcomeStatus(LowerCaseStrEnum):
    """Per-slice outcome of an operator pool bake (``minds-admin pool create``), as emitted in the JSON report."""

    SUCCEEDED = auto()
    FAILED = auto()


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
# template applies at bake time (see default-workspace-template's
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
