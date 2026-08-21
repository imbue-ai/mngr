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


# The second wire shape of the same concurrent-deploy race: instead of the app
# lock, the losing `modal deploy` can fail with "Function fu-<id> not found"
# when the winning deploy finalizes a new app version and the loser's
# freshly-minted function object vanishes mid-deploy. The id form (`fu-...`)
# is only ever produced by the deploy in flight, so this wording cannot mean a
# genuine misconfiguration (those reference functions by name, not id).
_DEPLOY_FUNCTION_VANISHED_RE = re.compile(r"function fu-\w+ not found", re.IGNORECASE)


def is_deploy_function_vanished_error(message: str) -> bool:
    """Check whether a `modal deploy` failure is the function-vanished flavor of the concurrent-modification race.

    Retrying the whole deploy succeeds once the racing deploy finishes (deploys
    of the same script are idempotent), so callers should treat this exactly
    like :func:`is_app_locked_error`.
    """
    return _DEPLOY_FUNCTION_VANISHED_RE.search(message) is not None


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


class ModalProxyConnectionError(ModalProxyError):
    """Raised when the Modal control plane could not be reached at all.

    Distinct from every other error here, which Modal produced *after* a
    connection was established: nothing was reached in this case, so whatever
    lives behind Modal is in an unknown state rather than a known-bad one. It
    is the shape a dropped network or a Modal outage takes, so consumers can
    treat it as "Modal is temporarily unavailable" instead of as a failure of
    the operation they asked for.
    """


class ModalProxyAppLockedError(ModalProxyError):
    """Raised when a Modal operation fails due to a concurrent modification of the same app.

    Modal serializes mutations to a single app, so concurrent operations on the
    same app (e.g. parallel ``modal deploy`` calls, or a deploy racing app
    creation) fail transiently. The race has two wire shapes: "The selected app
    is locked" (see ``is_app_locked_error``), and the losing deploy's fresh
    function id vanishing with "Function fu-<id> not found" when the winner
    finalizes a new app version (see ``is_deploy_function_vanished_error``).
    Both clear once the conflicting operation completes, so callers should
    retry with backoff.
    """
