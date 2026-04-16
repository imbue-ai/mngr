import re
from enum import auto
from typing import Any
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic import SecretStr
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.bootstrap import MINDS_ROOT_NAME_PATTERN


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


class AgentName(NonEmptyStr):
    """User-chosen name for an agent."""

    ...


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for workspace access."""

    ...


class CookieSigningKey(SecretStr):
    """Secret key used for signing authentication cookies."""

    ...


class ServerName(NonEmptyStr):
    """Name of a server run by an agent (e.g. 'web', 'api')."""

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


class MindsRootName(NonEmptyStr):
    """Shell-safe identifier that names a minds installation (e.g. 'minds', 'devminds').

    Becomes part of the data directory (``~/.<name>``) and mngr prefix (``<name>-``),
    so values must be restricted to characters that are safe in filesystem paths
    and tmux session names.
    """

    def __new__(cls, value: str) -> Self:
        if not re.fullmatch(MINDS_ROOT_NAME_PATTERN, value):
            raise ValueError("{} must match {!r}; got {!r}".format(cls.__name__, MINDS_ROOT_NAME_PATTERN, value))
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(pattern=MINDS_ROOT_NAME_PATTERN),
            serialization=core_schema.to_string_ser_schema(),
        )
