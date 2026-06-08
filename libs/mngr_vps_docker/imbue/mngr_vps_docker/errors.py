from imbue.mngr.errors import MngrError


class VpsDockerError(MngrError):
    """Base error for VPS Docker provider operations."""


class VpsProvisioningError(VpsDockerError):
    """Failed to provision a VPS instance."""


class VpsApiError(VpsDockerError):
    """Error from the VPS provider API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"VPS API error {status_code}: {message}")


class ContainerSetupError(VpsDockerError):
    """Raised when an outer-host container/image/snapshot setup step fails.

    The outer-host docker/rsync/snapshot helpers run their work inside
    ConcurrencyGroups, so a failure surfaces as a raw ConcurrencyExceptionGroup
    or ProcessError (e.g. ProcessTimeoutError) -- neither of which is a
    MngrError. This type wraps those concurrency-group failures (preserving the
    cause via ``raise ... from``) so callers can catch a single
    MngrError-derived type; without it, those exceptions slip past
    provider-level ``except MngrError`` cleanup clauses and leak half-built
    hosts. Inherits from VpsDockerError, which inherits from MngrError.
    """
