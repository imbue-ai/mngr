from imbue.mngr.errors import MngrError


class GcpError(MngrError):
    """Base exception for the GCP provider plugin."""


class GcpCredentialsError(GcpError, ValueError):
    """Google Application Default Credentials could not be resolved.

    Inherits ``ValueError`` so the backend's ``except ValueError`` (which wraps
    config-resolution failures into ``ProviderUnavailableError``) keeps
    catching it.
    """


class GcpProjectError(GcpError, ValueError):
    """No GCP project could be resolved from the config or the environment.

    Inherits ``ValueError`` for the same backend-catch reason as
    ``GcpCredentialsError``.
    """


class GcpZoneRegionMismatchError(GcpError, ValueError):
    """``default_zone`` does not lie within ``default_region``.

    Inherits ``ValueError`` for the same backend-catch reason as
    ``GcpCredentialsError``.
    """


class GcpStateBucketProvisioningError(GcpError):
    """``mngr gcp prepare`` could not create the GCS state bucket.

    Raised when the storage API rejects bucket creation (missing
    ``storage.buckets.create`` permission, an API failure, etc.). The bucket is
    the offline ``host_dir`` feature's only backing store, so a failure here is
    surfaced to the operator rather than silently leaving offline host_dir
    unavailable. Inherits ``GcpError`` -> ``MngrError`` so it renders as a clean
    ``Error: ...`` CLI message; the original ``GcsStateBucketError`` is preserved
    as the cause.
    """


class GcpStateBucketNotEmptyError(GcpError):
    """``mngr gcp cleanup`` refused to delete a GCS state bucket that still holds offline host state.

    Raised (without ``--force``) when the bucket still holds ``hosts/`` state from
    hosts whose instances are gone but whose ``delete_host_state`` never ran.
    Deleting it silently could drop offline records the operator still wants, so
    cleanup refuses and lets ``--force`` opt in. The sibling of
    ``ManagedResourcesExistError`` for the bucket teardown step; inherits
    ``GcpError`` -> ``MngrError`` so it renders identically to the other cleanup
    refusals.
    """


class InvalidGceIdentifierError(GcpError, ValueError):
    """A coerced GCE label value or instance name failed its validity check.

    Raised by the ``GceLabelValue`` / ``GceInstanceName`` constructors when the
    string handed to them does not satisfy GCE's identifier rules. In normal
    operation the coercion helpers always produce valid strings, so this firing
    signals a regression in that coercion rather than bad user input. Inherits
    ``ValueError`` for the same backend-catch reason as ``GcpCredentialsError``.
    """
