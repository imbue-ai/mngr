from imbue.mngr.errors import MngrError


class AzureProviderError(MngrError):
    """Base exception for the Azure provider plugin.

    Named ``AzureProviderError`` rather than ``AzureError`` to avoid colliding
    with ``azure.core.exceptions.AzureError`` from the Azure SDK.
    """


class AzureSubscriptionError(AzureProviderError, ValueError):
    """No Azure subscription could be resolved from the config or the environment.

    Inherits ``ValueError`` so the backend's ``except ValueError`` (which wraps
    config-resolution failures into ``ProviderUnavailableError``) keeps catching
    it.
    """
