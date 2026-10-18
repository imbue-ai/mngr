from imbue.mngr.errors import MngrError
from imbue.mngr_vps.primitives import VpsInstanceId


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


class VpsConfigError(VpsError, ValueError):
    """Raised when a VpsProviderConfig combines mutually-incompatible options."""


class VpsHostSizeUnknownError(VpsError):
    """Raised when a host's size cannot be determined from its record (no recorded shape and an unknown plan)."""


class VpsHostKeyMissingError(VpsError):
    """Raised when no client key that opens a host's VPS root is on disk.

    Read paths never mint the VPS-root client key: it is created once, at host
    creation, under the host's per-host key dir. A host whose per-host key is
    absent (and for which no legacy provider-wide key exists either) cannot be
    reached from this machine, so the operation fails here instead of opening
    an SSH connection that would be rejected.
    """

    user_help_text = (
        "This host was created on another device, or before per-host keys existed, so it has to be reached "
        "from the device that created it, or its record synced to this one."
    )


class VpsInstanceLeakedError(VpsProvisioningError):
    """A failed provision left a VPS instance behind that its cleanup could not destroy.

    Carries ``vps_instance_id`` so ``create_host``'s cleanup knows the instance
    survives and keeps the host's per-host key material (the only key the
    leaked VPS authorizes) instead of treating the VPS as never created.
    """

    def __init__(self, message: str, vps_instance_id: VpsInstanceId) -> None:
        self.vps_instance_id = vps_instance_id
        super().__init__(message)
