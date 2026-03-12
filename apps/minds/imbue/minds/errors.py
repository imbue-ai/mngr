import click


class MindError(click.ClickException):
    """Base exception for all minds errors.

    Inherits from click.ClickException so that mind errors are
    automatically formatted and displayed by click without needing
    manual re-raising as ClickException at every call site.
    """

    ...


class AgentAlreadyExistsError(MindError):
    """Raised when attempting to deploy a mind with a name that already exists."""

    ...


class SigningKeyError(MindError):
    """Raised when the cookie signing key cannot be loaded or created."""

    ...


class GitCloneError(MindError):
    """Raised when git clone fails."""

    ...


class GitInitError(MindError):
    """Raised when git init fails."""

    ...


class GitCommitError(MindError):
    """Raised when git add/commit fails."""

    ...


class MissingAgentTypeError(MindError):
    """Raised when no agent type is specified via CLI or minds.toml."""

    ...


class MngCommandError(MindError):
    """Raised when an mng CLI command fails."""

    ...
