from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr


class AgentName(NonEmptyStr):
    """User-chosen name for a changeling agent."""

    ...


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for changeling access."""

    ...


class CookieSigningKey(SecretStr):
    """Secret key used for signing authentication cookies."""

    ...


class ServerName(NonEmptyStr):
    """Name of a server run by a changeling agent (e.g. 'web', 'api')."""

    ...


class GitUrl(NonEmptyStr):
    """A git URL to clone (local path, file://, https://, or ssh)."""

    ...


class GitBranch(NonEmptyStr):
    """A git branch name to clone."""

    ...
