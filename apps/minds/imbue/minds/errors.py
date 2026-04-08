import click


class MindError(click.ClickException):
    """Base exception for all minds errors.

    Inherits from click.ClickException so that mind errors are
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
