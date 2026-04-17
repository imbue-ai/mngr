import re

_ENVIRONMENT_NOT_FOUND_RE = re.compile(r"^\s*Environment\b", re.IGNORECASE)


def is_environment_not_found_error(e: Exception) -> bool:
    """Check if a not-found exception indicates the Modal environment itself is gone.

    Modal uses one not-found exception type for both "path doesn't exist on volume"
    (expected during normal operations, e.g. listing a directory that hasn't been
    created yet) and "environment doesn't exist" (indicates the Modal environment
    is gone and should propagate to retry / error-handling layers). This helper
    distinguishes by matching messages of the form "Environment '<name>' not found"
    at the start of the exception message, so a path such as "/Environment/foo.json"
    in a path-level not-found is not misclassified as an environment error.
    """
    return _ENVIRONMENT_NOT_FOUND_RE.match(str(e)) is not None


class ModalProxyError(Exception):
    """Base error for modal_proxy operations."""


class ModalProxyTypeError(ModalProxyError):
    """Raised when a modal_proxy interface receives an incompatible implementation type."""


class ModalProxyAuthError(ModalProxyError):
    """Raised when Modal authentication fails."""


class ModalProxyNotFoundError(ModalProxyError):
    """Raised when a Modal resource is not found."""


class ModalProxyInvalidError(ModalProxyError):
    """Raised when an invalid argument is passed to Modal."""


class ModalProxyInternalError(ModalProxyError):
    """Raised on transient Modal internal errors."""


class ModalProxyRateLimitError(ModalProxyError):
    """Raised when a Modal API rate limit is exceeded."""


class ModalProxyRemoteError(ModalProxyError):
    """Raised on Modal remote execution errors."""
