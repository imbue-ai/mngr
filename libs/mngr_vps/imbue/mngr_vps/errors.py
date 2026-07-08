from imbue.mngr.errors import MngrError


class VpsError(MngrError):
    """Base error for VPS provider operations."""


class VpsProvisioningError(VpsError):
    """Failed to provision a VPS instance."""


class VpsApiError(VpsError):
    """Error from the VPS provider API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"VPS API error {status_code}: {message}")


class ManagedResourcesExistError(VpsError):
    """A provider ``cleanup`` command refused because managed resources still exist.

    Raised by ``refuse_if_managed_resources_exist`` when an operator runs a
    provider's ``cleanup`` (delete the shared SG / NSG / firewall rule / resource
    group) while mngr-managed instances are still present, so cleanup never
    strands a running agent. Inherits ``VpsError`` -> ``MngrError`` so it renders
    as a clean ``Error: ...`` CLI message, identically across the AWS / Azure /
    GCP providers (previously each raised a different exception type).
    """


class BareIsolationNotSupportedError(VpsError):
    """Raised when ``isolation=NONE`` is selected on a provider that does not support it.

    Bare placement needs a substrate that can stop and later restart the machine
    (the bare agent's idle action powers the VM off). Providers without a
    machine stop/start lifecycle (e.g. vultr/ovh) would strand the VM, so they
    reject ``isolation=NONE`` up front rather than create an unrecoverable host.
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
