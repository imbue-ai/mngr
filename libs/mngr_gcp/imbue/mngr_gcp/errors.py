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
