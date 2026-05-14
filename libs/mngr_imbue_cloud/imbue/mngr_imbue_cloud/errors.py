from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNetworkUnreachableError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.primitives import ProviderInstanceName


class ImbueCloudError(MngrError):
    """Base class for all imbue_cloud plugin errors."""


class ImbueCloudConnectorError(ImbueCloudError, ProviderNetworkUnreachableError):
    """Connector returned an unexpected response (5xx, malformed, network).

    The ``ProviderNetworkUnreachableError`` parent gives the discovery
    boundary a typed provider-unavailable class and lets programmatic
    consumers discriminate via ``exception_type``. Like any other provider
    failure it lands on ``ListResult.errors``.
    """

    def __init__(self, message: str, provider_name: str | None = None) -> None:
        if provider_name is not None:
            ProviderNetworkUnreachableError.__init__(self, ProviderInstanceName(provider_name), message)
        else:
            ImbueCloudError.__init__(self, message)


class ImbueCloudAuthError(ImbueCloudError, HostAuthenticationError, ProviderNotAuthorizedError):
    """Connector rejected the configured token (401/403).

    The ``ProviderNotAuthorizedError`` parent gives the discovery boundary
    a typed, user-actionable auth-failure class and lets programmatic
    consumers discriminate via ``exception_type``. Like any other provider
    failure it lands on ``ListResult.errors``. The ``HostAuthenticationError``
    parent lets per-host ``except HostAuthenticationError`` clauses match.
    """

    def __init__(self, message: str, provider_name: str | None = None) -> None:
        if provider_name is not None:
            ProviderNotAuthorizedError.__init__(self, ProviderInstanceName(provider_name), auth_help=message)
        else:
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
