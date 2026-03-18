from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr


class AgentName(NonEmptyStr):
    """User-chosen name for a mind agent."""

    ...


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for mind access."""

    ...


class CookieSigningKey(SecretStr):
    """Secret key used for signing authentication cookies."""

    ...


class ServerName(NonEmptyStr):
    """Name of a server run by a mind agent (e.g. 'web', 'api')."""

    ...


class GitUrl(NonEmptyStr):
    """A git URL to clone (local path, file://, https://, or ssh)."""

    ...


class GitBranch(NonEmptyStr):
    """A git branch name to clone."""

    ...


class GitCommitHash(NonEmptyStr):
    """A full git commit hash (40 hex characters)."""

    ...
