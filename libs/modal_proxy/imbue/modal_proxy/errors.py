import re

_ENVIRONMENT_NOT_FOUND_RE = re.compile(r"^Environment '[^']+' not found\b")

# Modal returns this when two operations modify the same app concurrently
# (e.g. parallel `modal deploy` calls, or a deploy racing app creation in the
# same app). The lock is held only for the duration of the conflicting
# operation, so the conflict is transient and safe to retry with backoff.
_APP_LOCKED_RE = re.compile(r"selected app is locked", re.IGNORECASE)


def is_app_locked_error(message: str) -> bool:
    """Check whether a Modal error message indicates a transient app lock.

    Modal serializes mutations to a single app; concurrent modifications (e.g.
    two ``modal deploy`` calls targeting the same app name, or a deploy racing
    app creation) fail with "The selected app is locked - probably due to a
    concurrent modification". The lock is released as soon as the conflicting
    operation finishes, so callers should retry with backoff rather than fail.
    """
    return _APP_LOCKED_RE.search(message) is not None


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


class ModalProxyInvalidError(ModalProxyError):
    """Raised when an invalid argument is passed to Modal."""


class ModalProxyInternalError(ModalProxyError):
    """Raised on transient Modal internal errors."""


class ModalProxyRateLimitError(ModalProxyError):
    """Raised when a Modal API rate limit is exceeded."""


class ModalProxyRemoteError(ModalProxyError):
    """Raised on Modal remote execution errors."""


class ModalProxyAppLockedError(ModalProxyError):
    """Raised when a Modal app is locked due to a concurrent modification.

    Modal serializes mutations to a single app, so concurrent operations on the
    same app (e.g. parallel ``modal deploy`` calls, or a deploy racing app
    creation) fail with "The selected app is locked". The lock is transient --
    it is released once the conflicting operation completes -- so callers should
    retry with backoff. See ``is_app_locked_error``.
    """
