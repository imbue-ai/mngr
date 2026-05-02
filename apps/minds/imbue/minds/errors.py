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
    """Raised when an mngr CLI command fails."""

    ...


class MalformedMngrOutputError(MindError, ValueError):
    """Raised when ``mngr list --format json`` produces output we can't parse.

    The right fix is to track down whichever process is leaking non-JSON to
    stdout (stdout is reserved for JSON data; logs belong on stderr) -- silently
    skipping the bad line would just hide the underlying problem.
    """

    ...


class MindsConfigError(MindError):
    """Raised when minds config cannot be parsed or validated."""

    ...


class TelegramError(MindError):
    """Base exception for all telegram-related errors."""

    ...


class TelegramCredentialError(TelegramError, ValueError):
    """Raised when telegram credentials are invalid or missing."""

    ...


class TelegramCredentialExtractionError(TelegramError):
    """Raised when credential extraction from the browser fails."""

    ...


class TelegramBotCreationError(TelegramError):
    """Raised when bot creation via BotFather fails."""

    ...
