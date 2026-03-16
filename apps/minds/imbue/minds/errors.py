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


class MngCommandError(MindError):
    """Raised when an mng CLI command fails."""

    ...


class VendorError(MindError):
    """Raised when vendoring mng into a mind repo fails."""

    ...
