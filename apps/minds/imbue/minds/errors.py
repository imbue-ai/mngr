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


class GitOperationError(MindError):
    """Raised when a git operation fails during mind management."""

    ...


class VendorError(GitOperationError):
    """Raised when vendoring a repo into a mind fails."""

    ...


class ParentTrackingError(GitOperationError):
    """Raised when a parent tracking git operation fails."""

    ...


class DirtyRepoError(VendorError):
    """Raised when a local vendor repo has uncommitted changes or untracked files."""

    ...
