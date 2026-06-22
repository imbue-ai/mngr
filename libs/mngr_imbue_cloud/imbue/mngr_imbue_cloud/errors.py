from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import MngrError


class ImbueCloudError(MngrError):
    """Base class for all imbue_cloud plugin errors."""


class ImbueCloudConnectorError(ImbueCloudError):
    """Raised when the remote_service_connector returns an unexpected response."""


class SliceBakeTerminatedError(ImbueCloudError):
    """Raised in the bake's main thread when a SIGTERM/SIGINT arrives, to trigger cleanup."""


class ImbueCloudAuthError(ImbueCloudError, HostAuthenticationError):
    """Raised when authentication is missing or refresh fails."""

    def __init__(self, message: str) -> None:
        ImbueCloudError.__init__(self, message)


class ImbueCloudLeaseUnavailableError(ImbueCloudError):
    """Raised when the connector returns 503 (no matching pool host)."""


class FastPathUnavailableError(ImbueCloudError):
    """Raised when ``fast_mode=require`` finds no exact-attribute pool match.

    Distinct from ``ImbueCloudLeaseUnavailableError`` (which means the pool is
    genuinely empty): this signals that the fast/adopt path specifically could
    not be satisfied, so a caller (e.g. minds) can fall back to the slow path
    by re-running with ``fast_mode=prevent``.
    """


class ImbueCloudKeyError(ImbueCloudError):
    """Raised when a LiteLLM key operation fails."""


class ImbueCloudTunnelError(ImbueCloudError):
    """Raised when a Cloudflare tunnel operation fails."""


class ImbueCloudPaidListError(ImbueCloudError):
    """Raised when a paid-list (paid domains / emails) admin operation fails."""


class PoolHostNotMatchedError(ImbueCloudError):
    """Raised when create_agent is invoked on a leased host that has no pre-baked agent or has more than one."""


class AccountNotConfiguredError(ImbueCloudError):
    """Raised when the requested account has no provider instance entry."""


class ImbueCloudBucketError(ImbueCloudError):
    """Raised when an R2 bucket or bucket-key operation fails."""


class ImbueCloudBucketNotEmptyError(ImbueCloudBucketError):
    """Raised when destroying a bucket that still contains objects."""


class ImbueCloudBucketExistsError(ImbueCloudBucketError):
    """Raised when creating a bucket whose derived name already exists."""


class ImbueCloudBucketNotFoundError(ImbueCloudBucketError):
    """Raised when referencing a bucket that does not exist (or is not the caller's)."""


class ImbueCloudBucketLimitError(ImbueCloudBucketError):
    """Raised when the account is already at the per-account bucket cap."""


class OvhCatalogPricingError(ImbueCloudError):
    """Raised when an OVH catalog plan or add-on cannot be priced (missing entry or no month-to-month price)."""


class BareMetalConfigError(ImbueCloudError, ValueError):
    """Raised when a bare-metal server / slice has an invalid configuration (bad size, disk count, etc.)."""


class SliceCapacityError(ImbueCloudError):
    """Raised when no bare-metal server has free slots (or ports) to allocate a new slice."""


class BareMetalProvisioningError(ImbueCloudError):
    """Raised when ordering, installing, or carving a bare-metal server / slice fails."""


class SliceReserveOutputError(BareMetalProvisioningError):
    """Raised when the on-box slice reservation script produces no/garbled port output."""


class InvalidBuildArgError(ImbueCloudError, ValueError):
    """Raised when a recognized imbue_cloud build arg has a malformed value."""


class RepoIdentityError(ImbueCloudError, ValueError):
    """Raised when a repository's canonical identity cannot be established.

    Covers an empty/malformed URL, a local path whose ``origin`` remote is
    missing, and a local checkout on a detached HEAD. Callers decide how to
    surface it: the fast path wraps it as ``FastPathUnavailableError`` (so it
    falls back to the slow path); the bake tooling lets it fail the command.
    """


class FixedAgentIdError(ImbueCloudError, ValueError):
    """Raised when a caller requests an agent id that conflicts with the lease's pre-baked id."""


class ClaudeConfigPatchError(ImbueCloudError, RuntimeError):
    """Raised when patching the claude config on a leased imbue_cloud host fails."""
