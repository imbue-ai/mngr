from enum import auto

from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr


class OutputFormat(UpperCaseStrEnum):
    """Output format for command results on stdout."""

    HUMAN = auto()
    JSON = auto()
    JSONL = auto()


class LaunchMode(UpperCaseStrEnum):
    """How a workspace agent should be launched."""

    LOCAL = auto()
    CLOUD = auto()
    DEV = auto()
    LIMA = auto()
    LEASED = auto()


class AgentName(NonEmptyStr):
    """User-chosen name for an agent."""

    ...


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for workspace access."""

    ...


class CookieSigningKey(SecretStr):
    """Secret key used for signing authentication cookies."""

    ...


class ServiceName(NonEmptyStr):
    """Name of a service run by an agent (e.g. 'web', 'api')."""

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


class ApiKeyHash(NonEmptyStr):
    """SHA-256 hex digest of an agent's API key."""

    ...
