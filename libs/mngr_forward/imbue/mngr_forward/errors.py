from imbue.mngr.errors import MngrError


class MngrForwardError(MngrError):
    """Base class for all mngr_forward plugin errors."""


class ForwardManualConfigError(MngrForwardError):
    """Raised on bad CLI option combinations or empty manual snapshots.

    Examples: ``--no-observe --service NAME`` (mutex violation), or
    ``--no-observe`` against an empty post-filter ``mngr list`` snapshot.
    """


class ForwardAuthError(MngrForwardError):
    """Raised when the cookie signing key cannot be read or written."""


class ForwardSubprocessError(MngrForwardError):
    """Raised when an ``mngr observe`` / ``mngr event`` subprocess fails to spawn."""
