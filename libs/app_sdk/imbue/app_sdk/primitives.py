import re
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.app_sdk.errors import InvalidIdentityValueError

# A value that names a file on disk is held to one filename-safe alphabet.
FILENAME_SAFE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A name known only from a hand-typed author prefix may hold any printable text; its key form holds it to at most
# this many characters.
MAX_PARTICIPANT_NAME_LENGTH: Final[int] = 60
# Every form of a participant key is filename-safe and URL-path-safe, since a key names a file and sits in a path:
# no slash anywhere, no control character, and no "#", "?", or "%" in an address.
_PARTICIPANT_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:owner|workspace-agent|user:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|email:[^\s/#?%:@]+@[^\s/#?%@]+"
    r"|agent:[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    rf"|name:[^\n/\x00-\x1f]{{1,{MAX_PARTICIPANT_NAME_LENGTH}}})$"
)
OWNER_PARTICIPANT_KEY_VALUE: Final[str] = "owner"
WORKSPACE_AGENT_PARTICIPANT_KEY_VALUE: Final[str] = "workspace-agent"


def _validated_string_schema(cls: type) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class UserId(str):
    """A signed-in account's user id as the identity header carries it; it names the user's state files, so it is
    held to a filename-safe alphabet."""

    def __new__(cls, value: str) -> Self:
        if not FILENAME_SAFE_PATTERN.fullmatch(value):
            raise InvalidIdentityValueError(f"invalid user id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _validated_string_schema(cls)


class ParticipantKey(str):
    """Who a message, a desktop, a client, or a roster entry belongs to: ``owner``, ``workspace-agent``,
    ``user:<user_id>``, ``email:<address>``, ``agent:<agent_id>``, or ``name:<name>``, each filename-safe (no slash,
    no control character)."""

    def __new__(cls, value: str) -> Self:
        if not _PARTICIPANT_KEY_PATTERN.fullmatch(value):
            raise InvalidIdentityValueError(f"invalid participant key {value!r}")
        return super().__new__(cls, value.strip())

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _validated_string_schema(cls)


OWNER_PARTICIPANT_KEY: Final[ParticipantKey] = ParticipantKey(OWNER_PARTICIPANT_KEY_VALUE)
WORKSPACE_AGENT_PARTICIPANT_KEY: Final[ParticipantKey] = ParticipantKey(WORKSPACE_AGENT_PARTICIPANT_KEY_VALUE)
