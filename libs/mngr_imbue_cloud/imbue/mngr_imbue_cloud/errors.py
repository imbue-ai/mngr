from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import MngrError


class ImbueCloudError(MngrError):
    """Base class for all imbue_cloud plugin errors."""


class ImbueCloudConnectorError(ImbueCloudError):
    """Raised when the remote_service_connector returns an unexpected response."""


class ImbueCloudUnreachableError(ImbueCloudConnectorError):
    """Raised when the connector could not be reached at the transport level (after bounded retries).

    Distinct from its parent so callers can tell "no response ever arrived"
    (DNS failure, connect/read timeout -- the flaky-network case, worth
    surfacing as ProviderUnavailableError) apart from "the connector answered
    with an error status".
    """


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


class ImbueCloudShareError(ImbueCloudError):
    """Raised when a self-hosted share operation fails."""


class ImbueCloudPaidListError(ImbueCloudError):
    """Raised when a paid-list (paid domains / emails) admin operation fails."""


class ImbueCloudQuotaExceededError(ImbueCloudError):
    """Raised when the connector refuses an operation because a quota entitlement is exhausted.

    Carries the structured detail from the connector's 403 (``code:
    quota_exceeded``) so callers can render "N of M used" without parsing
    the message text.
    """

    def __init__(self, message: str, entitlement: str, limit: float, current: float) -> None:
        super().__init__(message)
        self.entitlement = entitlement
        self.limit = limit
        self.current = current


class ImbueCloudEmailNotVerifiedError(ImbueCloudError):
    """Raised when the connector refuses an action because the account's email is unverified.

    Carries the structured detail from the connector's 403 (``code:
    email_not_verified``) so callers (e.g. the minds desktop client) can
    respond with a contextual "verify your email" prompt instead of a
    generic failure.
    """

    def __init__(self, message: str, email: str | None) -> None:
        super().__init__(message)
        self.email = email


class ImbueCloudAccountSuspendedError(ImbueCloudError):
    """Raised when the connector refuses an action because the account is suspended.

    Carries the connector's user-facing message from the structured 403
    (``code: account_suspended``), which includes the support contact --
    the operator-recorded reason is never sent to clients.
    """


class ImbueCloudAccountError(ImbueCloudError):
    """Raised when an account (plan / entitlements / usage) operation fails."""


class ImbueCloudCleanupGrantBudgetError(ImbueCloudError):
    """Raised when the connector refuses a storage-cleanup grant: the failed-grant budget is exhausted.

    Carries the structured detail from the connector's 403 (``code:
    cleanup_grant_budget_exhausted``). Grants that actually reduced usage
    never count against the budget, so this only fires after repeated grants
    that freed nothing.
    """

    def __init__(self, message: str, limit: int, current: int, window_hours: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.current = current
        self.window_hours = window_hours


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


class ImbueCloudSyncError(ImbueCloudError):
    """Raised when a workspace-record / key-bundle sync operation fails."""


class ImbueCloudSyncConflictError(ImbueCloudSyncError):
    """Raised on a 409 from a record push (revision CAS or active-agent conflict).

    ``stored_record`` carries the server's current row (as a plain dict) when
    the conflict was a revision CAS failure, so the caller can merge and retry;
    it is None for an active-agent uniqueness conflict.
    """

    def __init__(self, message: str, stored_record: dict[str, object] | None) -> None:
        super().__init__(message)
        self.stored_record = stored_record


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


class BoxImageCacheError(ImbueCloudError):
    """Raised when a box-local cached-default-workspace-template-image operation (lock, save, load, disk) fails."""


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


class AdoptionError(ImbueCloudError):
    """Raised when adopting a leased slice (reconciler install / key rotation / verification) fails."""


class HostKeyDriftError(AdoptionError):
    """Raised when an adopted endpoint serves a key that matches neither its pin nor a pending rotation.

    Somebody other than this user's devices re-keyed the host (e.g. an operator
    re-key, or a rebuild this device has not recorded). The device correctly
    refuses to trust the new key; the user re-adopts (or re-syncs) to recover.
    """


class WireEnumMissingUnknownMemberError(ImbueCloudError, TypeError):
    """Raised when a WireEnum subclass fails to define the UNKNOWN member its coercion contract requires."""


# The standard remedy text for the client-too-old refusal, shown when neither
# the connector's HTTP 426 detail nor the plugin's stderr carries a message of
# its own. Shared so the plugin and the desktop wrapper can never diverge.
CLIENT_TOO_OLD_FALLBACK_MESSAGE = "This app version is no longer supported; please update it."


class ImbueCloudClientTooOldError(ImbueCloudError):
    """Raised when the connector refuses a request because this client version is no longer supported.

    Carries the structured detail from the connector's HTTP 426 (``code:
    client_too_old``). Deterministic -- retrying cannot succeed until the
    client updates -- so callers surface an "update the app" prompt instead
    of a generic failure. ``min_version`` / ``sunset_date`` are None when the
    server's refusal did not carry them.
    """

    def __init__(self, message: str, min_version: str | None, sunset_date: str | None) -> None:
        super().__init__(message)
        self.min_version = min_version
        self.sunset_date = sunset_date


class ImbueCloudRecordFormatTooNewError(ImbueCloudSyncError):
    """Raised when a record push is refused because the stored row's record_format is newer.

    The connector's structured 409 (``code: record_format_too_new``): the
    stored record's semantics postdate this client, so modifying it could
    corrupt meaning the client cannot see. The record stays readable; the
    remedy is updating the app.
    """


class UnrecognizedWorkspaceStatusError(ImbueCloudError):
    """Raised when a state-changing operation targets a workspace whose status this client cannot interpret.

    The wire status coerced to ``WorkspaceStatus.UNKNOWN`` (a newer server's
    vocabulary). Observation stays available, but driving a lifecycle
    transition from an unintelligible state would act blindly, so the client
    refuses with an "update the app" message instead.
    """


class WorkspacesEndpointUnavailableError(ImbueCloudConnectorError):
    """Raised when the connector predates the /workspaces lifecycle endpoints."""


class WorkspaceStartFailedError(ImbueCloudError):
    """Raised when a workspace start ended in failure server-side (row back on stopped)."""


class WorkspaceStartTimeoutError(ImbueCloudError):
    """Raised when a workspace start did not reach running within the client's poll window."""
