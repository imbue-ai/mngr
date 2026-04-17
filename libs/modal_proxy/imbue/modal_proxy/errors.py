def is_environment_not_found_error(e: BaseException) -> bool:
    """Check if a not-found exception indicates the Modal environment itself is gone.

    Modal uses one not-found exception type for both "path doesn't exist on volume"
    (expected during normal operations, e.g. listing a directory that hasn't been
    created yet) and "environment doesn't exist" (indicates the Modal environment
    is gone and should propagate to retry / error-handling layers). This helper
    distinguishes by checking the exception message.

    Works on both raw modal.exception.NotFoundError (pre-translation) and
    ModalProxyNotFoundError (post-translation), since _translate_modal_error
    copies the message via str(e).
    """
    return "Environment" in str(e)


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
