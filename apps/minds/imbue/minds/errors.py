import click


class MindError(click.ClickException):
    """Base exception for all minds errors.

    Inherits from click.ClickException so that minds errors are
    automatically formatted and displayed by click without needing
    manual re-raising as ClickException at every call site.
    """

    ...


class SigningKeyError(MindError):
    """Raised when the cookie signing key cannot be loaded or created."""

    ...


class GitCloneError(MindError):
    """Raised when git clone fails."""

    ...


class GitOperationError(MindError):
    """Raised when a git operation (other than clone) fails."""

    ...


class MngrCommandError(MindError):
    """Raised when an mngr CLI command fails (timed out, exited nonzero, or could not be launched)."""

    def __init__(self, message: str, *, error_class: str | None = None) -> None:
        super().__init__(message)
        # mngr's exception class name, parsed from a structured JSONL ``error``
        # event when available (e.g. ``FastPathUnavailableError``). Lets callers
        # branch on the failure *type* without matching human-formatted text.
        self.error_class = error_class


class MngrCommandTimeoutError(MngrCommandError):
    """Raised when an mngr CLI command did not finish within its timeout.

    A distinct subclass so callers can tell "the command ran and failed" (still
    a ``MngrCommandError``, with a body to inspect) apart from "the command
    never completed". The recovery host-health probe keys on this: a listing
    that times out is evidence the provider/network is unreachable, not that the
    host is reachable-but-wedged, so it must not offer a destructive restart.
    """

    ...


class MalformedMngrOutputError(MindError, ValueError):
    """Raised when ``mngr list --format json`` produces output we can't parse.

    The right fix is to track down whichever process is leaking non-JSON to
    stdout (stdout is reserved for JSON data; logs belong on stderr) -- silently
    skipping the bad line would just hide the underlying problem.
    """

    ...


class InvalidJsonBodyError(MindError, ValueError):
    """Raised when a request body is missing or not valid JSON.

    Subclasses ``ValueError`` so the desktop client's request handlers can keep
    catching ``(json.JSONDecodeError, ValueError)`` around body parsing.
    """

    ...


class MindsConfigError(MindError):
    """Raised when minds config cannot be parsed or validated."""

    ...


class DeployLifecycleConfigError(MindError, ValueError):
    """Raised when a deploy lifecycle config combination is invalid."""

    ...


class EnvelopeStreamConsumerError(MindError, RuntimeError):
    """Raised when the envelope stream consumer is used out of lifecycle order."""

    ...


class BackupProvisioningError(MindError):
    """Raised when configuring restic backups for a workspace fails."""

    ...


class TelegramError(MindError):
    """Base exception for all telegram-related errors."""

    ...


class TelegramCredentialError(TelegramError, ValueError):
    """Raised when telegram credentials are invalid or missing."""

    ...


class TelegramCredentialExtractionError(TelegramError, ValueError):
    """Raised when credential extraction from the browser fails."""

    ...


class TelegramBotCreationError(TelegramError):
    """Raised when bot creation via BotFather fails."""

    ...
