import click


class ChangelingError(click.ClickException):
    """Base exception for all changelings errors.

    Inherits from click.ClickException so that changeling errors are
    automatically formatted and displayed by click without needing
    manual re-raising as ClickException at every call site.
    """

    ...


class AgentAlreadyExistsError(ChangelingError):
    """Raised when attempting to deploy a changeling with a name that already exists."""

    ...


class SigningKeyError(ChangelingError):
    """Raised when the cookie signing key cannot be loaded or created."""

    ...


class GitCloneError(ChangelingError):
    """Raised when git clone fails."""

    ...


class GitInitError(ChangelingError):
    """Raised when git init fails."""

    ...


class GitCommitError(ChangelingError):
    """Raised when git add/commit fails."""

    ...


class MissingSettingsError(ChangelingError):
    """Raised when a changeling repo is missing .mng/settings.toml."""

    ...


class MngCommandError(ChangelingError):
    """Raised when an mng CLI command fails."""

    ...
