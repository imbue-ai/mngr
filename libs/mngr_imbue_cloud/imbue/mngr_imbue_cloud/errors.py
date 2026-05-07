from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import MngrError


class ImbueCloudError(MngrError):
    """Base class for all imbue_cloud plugin errors."""


class ImbueCloudConnectorError(ImbueCloudError):
    """Raised when the remote_service_connector returns an unexpected response."""


class ImbueCloudAuthError(ImbueCloudError, HostAuthenticationError):
    """Raised when authentication is missing or refresh fails."""

    def __init__(self, message: str) -> None:
        ImbueCloudError.__init__(self, message)


class ImbueCloudLeaseUnavailableError(ImbueCloudError):
    """Raised when the connector returns 503 (no matching pool host)."""


class ImbueCloudKeyError(ImbueCloudError):
    """Raised when a LiteLLM key operation fails."""


class ImbueCloudTunnelError(ImbueCloudError):
    """Raised when a Cloudflare tunnel operation fails."""


class PoolHostNotMatchedError(ImbueCloudError):
    """Raised when create_agent is invoked on a leased host that has no pre-baked agent or has more than one."""


class AccountNotConfiguredError(ImbueCloudError):
    """Raised when the requested account has no provider instance entry."""
