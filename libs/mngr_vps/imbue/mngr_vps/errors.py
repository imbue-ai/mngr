from imbue.mngr.errors import MngrError


class VpsError(MngrError):
    """Base error for VPS Docker provider operations."""


class VpsProvisioningError(VpsError):
    """Failed to provision a VPS instance."""


class VpsApiError(VpsError):
    """Error from the VPS provider API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"VPS API error {status_code}: {message}")


class BareIsolationNotYetSupportedError(VpsError):
    """Raised when ``isolation=NONE`` is selected before the bare realizer ships.

    The realizer seam lands first with only the Docker path implemented; the
    bare (no-container) realizer arrives in a later step. Until then, selecting
    ``IsolationMode.NONE`` fails fast with this error rather than silently
    falling back to the container path.
    """


class ContainerSetupError(VpsError):
    """Raised when an outer-host container/image/snapshot setup step fails.

    The outer-host docker/rsync/snapshot helpers run their work inside
    ConcurrencyGroups, so a failure surfaces as a raw ConcurrencyExceptionGroup
    or ProcessError (e.g. ProcessTimeoutError) -- neither of which is a
    MngrError. This type wraps those concurrency-group failures (preserving the
    cause via ``raise ... from``) so callers can catch a single
    MngrError-derived type; without it, those exceptions slip past
    provider-level ``except MngrError`` cleanup clauses and leak half-built
    hosts. Inherits from VpsError, which inherits from MngrError.
    """
