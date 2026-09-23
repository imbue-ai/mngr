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


class ForwardTLSError(MngrForwardError):
    """Raised when the TLS material (local CA or server leaf) cannot be built or loaded."""


class ForwardTrustError(MngrForwardError):
    """Raised when the local CA cannot be installed into the platform trust stores."""


class ForwardRequestHeadersError(MngrForwardError, ValueError):
    """Raised when a request-headers file entry is malformed.

    A key that is neither an agent id nor ``"*"``, a header name that is not a
    valid token, or a framing / hop-by-hop header the proxy may not set.
    """
