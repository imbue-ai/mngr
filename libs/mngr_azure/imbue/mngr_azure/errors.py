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


class InvalidAzureIdentifierError(AzureProviderError, ValueError):
    """A coerced Azure VM resource name failed its validity check.

    Raised by the ``AzureVmName`` constructor when the string handed to it does
    not satisfy Azure's VM-name rules. In normal operation ``_make_vm_name``
    always produces a valid string, so this firing signals a regression in that
    coercion rather than bad user input. Inherits ``ValueError`` for the same
    backend-catch reason as ``AzureSubscriptionError``.
    """
