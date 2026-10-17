from imbue.mngr.errors import MngrError
from imbue.mngr_vps.errors import VpsApiError


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


class AzureVmSizeUnavailableError(AzureProviderError, VpsApiError):
    """Azure refused to launch the requested VM size in the region for this subscription (``SkuNotAvailable``).

    A per-subscription restriction rather than a transient capacity outage:
    new pay-as-you-go subscriptions are commonly barred from the B-series and
    the older D-series sizes in popular regions, and the request is refused
    even when the subscription's vCPU quota for the family is unused.
    """

    def __init__(self, *, vm_size: str, region: str, status_code: int, azure_message: str) -> None:
        self.vm_size = vm_size
        self.region = region
        super().__init__(
            status_code,
            f"Azure will not launch VM size {vm_size!r} in region {region!r} for this subscription "
            "(SkuNotAvailable). This is a per-subscription restriction, not a passing capacity shortage: new "
            "pay-as-you-go subscriptions are usually barred from the B-series and older D-series sizes in popular "
            "regions, even with unused vCPU quota. Pick another size with `-b --azure-vm-size=<size>` (the Dsv6 "
            "and Ddsv6 families, e.g. Standard_D2s_v6, are commonly allowed), another region, or request access "
            f"under Azure Portal > Subscriptions > Usage + quotas. Azure said: {azure_message}",
        )
