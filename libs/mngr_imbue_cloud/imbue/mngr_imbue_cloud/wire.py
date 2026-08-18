"""Bases for parsing remote_service_connector responses forward-compatibly.

Connector responses are cross-version wire data: the server deploys
continuously while shipped clients (the minds desktop app bundles this
plugin) update on their own cadence, so an already-released client routinely
parses responses produced by a *newer* server. Everything that parses a
connector response therefore descends from the bases here:

- :class:`WireModel` ignores unknown fields, so one additive server field
  never breaks an already-shipped client. Required fields stay required, so
  a *removed* or renamed response field still fails loudly.
- :class:`WireEnum` coerces unrecognized values to the enum's ``UNKNOWN``
  member at the parse boundary, so one new server-side enum value degrades
  ("shown but not actionable") instead of raising at every consumer.
- :func:`validate_wire` / :func:`parse_wire_entries` are the parse
  entrypoints: they add drift observability (unknown top-level keys are
  debug-logged once per shape) and the list semantics that keep a schema
  break from masquerading as an empty listing.

A repo-wide meta ratchet bans WireModel subclasses from re-tightening
``extra`` to ``"forbid"`` (mirroring the EventEnvelope guard), and the
connector's golden compat test proves every response still parses for every
shipped client version inside the support window.
"""

from collections.abc import Mapping
from typing import Any
from typing import TypeVar

from loguru import logger
from pydantic import ConfigDict

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_imbue_cloud.errors import WireEnumMissingUnknownMemberError


class WireModel(FrozenModel):
    """Base for models parsed from connector responses: frozen, but tolerant of unknown fields."""

    model_config = ConfigDict(extra="ignore")


class WireEnum(LowerCaseStrEnum):
    """Base for enums whose values arrive on the connector wire.

    Subclasses must define an ``UNKNOWN`` member; any unrecognized wire value
    coerces to it instead of raising, so a new server-side value degrades
    gracefully in already-shipped clients.
    """

    @classmethod
    def _missing_(cls, value: object) -> "WireEnum | None":
        unknown_member = cls.__members__.get("UNKNOWN")
        if unknown_member is None:
            raise WireEnumMissingUnknownMemberError(f"{cls.__name__} must define an UNKNOWN member to be a WireEnum")
        # Normalize before giving up: a case/whitespace variant of a known
        # value is semantically known, and coercing it to UNKNOWN would
        # needlessly degrade it.
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        _log_unknown_enum_value_once(cls.__name__, value)
        return unknown_member


WireModelT = TypeVar("WireModelT", bound=WireModel)

# Shapes already reported this process, so steady-state polling against a
# newer server logs each drift exactly once instead of on every request.
_LOGGED_UNKNOWN_FIELD_SHAPES: set[tuple[str, frozenset[str]]] = set()
_LOGGED_UNKNOWN_ENUM_VALUES: set[tuple[str, str]] = set()


def _log_unknown_enum_value_once(enum_name: str, value: object) -> None:
    dedupe_key = (enum_name, str(value))
    if dedupe_key in _LOGGED_UNKNOWN_ENUM_VALUES:
        return
    _LOGGED_UNKNOWN_ENUM_VALUES.add(dedupe_key)
    logger.debug("Coerced unrecognized {} wire value {!r} to UNKNOWN (a newer server?)", enum_name, value)


def _log_unknown_fields_once(model_cls: type[WireModel], body: Mapping[Any, Any]) -> None:
    unknown_keys = frozenset(str(key) for key in body.keys()) - frozenset(model_cls.model_fields.keys())
    if not unknown_keys:
        return
    dedupe_key = (model_cls.__name__, unknown_keys)
    if dedupe_key in _LOGGED_UNKNOWN_FIELD_SHAPES:
        return
    _LOGGED_UNKNOWN_FIELD_SHAPES.add(dedupe_key)
    logger.debug("Ignored unknown {} response field(s) {} (a newer server?)", model_cls.__name__, sorted(unknown_keys))


def validate_wire(model_cls: type[WireModelT], body: object) -> WireModelT:
    """Parse one connector response body into a wire model, debug-logging ignored fields once per shape."""
    parsed = model_cls.model_validate(body)
    if isinstance(body, Mapping):
        _log_unknown_fields_once(model_cls, body)
    return parsed


def parse_wire_entries(
    model_cls: type[WireModelT],
    body: object,
    # Names the endpoint in log/error messages (e.g. "GET /workspaces").
    context: str,
    error_cls: type[Exception],
) -> list[WireModelT]:
    """Parse a list response entry-by-entry without letting a schema break look like an empty listing.

    One unparseable entry is skipped with a warning (the rest of the listing
    survives). But a non-list body, or a non-empty listing where *every*
    entry fails, raises ``error_cls``: that shape means the client cannot
    read this server's responses at all, and returning ``[]`` would report a
    fleet of zero -- which downstream code cannot distinguish from a genuine
    empty result.
    """
    if not isinstance(body, list):
        raise error_cls(f"{context}: expected a JSON list, got {type(body).__name__}")
    parsed_entries: list[WireModelT] = []
    failed_count = 0
    for entry in body:
        try:
            parsed_entries.append(validate_wire(model_cls, entry))
        except ValueError as exc:
            failed_count += 1
            logger.warning(
                "Skipped an unparseable {} entry from {} (a newer server?): {}", model_cls.__name__, context, exc
            )
    if body and not parsed_entries:
        raise error_cls(
            f"{context}: all {failed_count} entries failed {model_cls.__name__} validation; "
            "refusing to report an empty listing (this client is likely too old for the server)"
        )
    return parsed_entries
