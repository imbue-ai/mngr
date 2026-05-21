import re

_ENVIRONMENT_NOT_FOUND_RE = re.compile(r"^Environment '[^']+' not found\b")


def is_environment_not_found_error(e: Exception) -> bool:
    """Check if a not-found exception indicates the Modal environment itself is gone.

    Modal uses one not-found exception type for both "path doesn't exist on volume"
    (expected during normal operations, e.g. listing a directory that hasn't been
    created yet) and "environment doesn't exist" (indicates the Modal environment
    is gone and should propagate to retry / error-handling layers). This helper
    matches the exact Modal SDK wording for the environment case:
    ``Environment '<name>' not found``.
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


class ModalProxyPermissionDeniedError(ModalProxyError):
    """Raised when Modal denies access to a resource.

    Modal's per-user permission entries are propagated asynchronously, so a
    just-created environment (or a just-deleted volume) will report this
    error for several seconds before the permission system catches up.
    Callers that are in the middle of a creation/teardown flow should retry
    this error with backoff.
    """


class ModalProxyInvalidError(ModalProxyError):
    """Raised when an invalid argument is passed to Modal."""


class ModalProxyInternalError(ModalProxyError):
    """Raised on transient Modal internal errors."""


class ModalProxyRateLimitError(ModalProxyError):
    """Raised when a Modal API rate limit is exceeded."""


class ModalProxyRemoteError(ModalProxyError):
    """Raised on Modal remote execution errors."""
