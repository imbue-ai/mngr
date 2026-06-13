"""Click ``ParamType`` adapters that parse mngr address strings into typed values.

Each ParamType wraps one of the parsers in :mod:`imbue.mngr.api.address_parsers`.
Click invokes these during argument parsing, so command bodies receive typed
addresses (``AgentAddress``, ``HostAddress``, ``NewAgentLocation``,
``HostLocationAddress``) rather than raw strings.

Use the module-level singletons (``AGENT_ADDRESS``, ``HOST_ADDRESS``, ...) as
the ``type=`` value on a ``@click.option`` or ``@click.argument`` decorator.
"""

from typing import Any

import click

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.address_parsers import parse_agent_name_or_id
from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.api.address_parsers import parse_host_address
from imbue.mngr.api.address_parsers import parse_host_location_address
from imbue.mngr.api.address_parsers import parse_host_name_or_id
from imbue.mngr.api.address_parsers import parse_new_agent_location
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameOrId
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import HostNameOrId
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import NewAgentLocation


def _convert_with_user_input_error(
    fn,
    value: str,
    param: click.Parameter | None,
    ctx: click.Context | None,
):
    """Run ``fn(value)`` and translate :class:`UserInputError` into a Click parse failure."""
    try:
        return fn(value)
    except UserInputError as e:
        raise click.BadParameter(str(e), ctx=ctx, param=param) from e


class AgentAddressParamType(click.ParamType):
    """Click param type for ``NAME[@HOST[.PROVIDER]]`` agent address strings."""

    name = "agent_address"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> AgentAddress:
        if isinstance(value, AgentAddress):
            return value
        return _convert_with_user_input_error(parse_agent_address, value, param, ctx)


class HostAddressParamType(click.ParamType):
    """Click param type for ``HOST[.PROVIDER]`` host address strings."""

    name = "host_address"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> HostAddress:
        if isinstance(value, HostAddress):
            return value
        return _convert_with_user_input_error(parse_host_address, value, param, ctx)


class AgentOrHostAddressParamType(click.ParamType):
    """Click param type for an :class:`AgentOrHostAddress`.

    Accepts ``AGENT[@HOST[.PROVIDER]]`` for agents and ``@HOST[.PROVIDER]`` or
    ``HOST.PROVIDER`` (or a bare :class:`HostId`) for hosts. See
    :func:`parse_agent_or_host_address` for the full disambiguation rules.
    """

    name = "agent_or_host_address"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> AgentOrHostAddress:
        if isinstance(value, (AgentAddress, HostAddress)):
            return value
        return _convert_with_user_input_error(parse_agent_or_host_address, value, param, ctx)


class NewAgentLocationParamType(click.ParamType):
    """Click param type for ``mngr create``'s ``[NAME][@HOST[.PROVIDER]][:PATH]`` argument."""

    name = "new_agent_location"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> NewAgentLocation:
        if isinstance(value, NewAgentLocation):
            return value
        return _convert_with_user_input_error(parse_new_agent_location, value, param, ctx)


class HostLocationAddressParamType(click.ParamType):
    """Click param type for ``[NAME[@HOST[.PROVIDER]]][:PATH]`` source/target arguments.

    Used by commands that designate "a location on any host" -- e.g. the
    ``--from`` argument of ``mngr create``, the ``SOURCE``/``DESTINATION``
    arguments of ``mngr rsync``, the ``TARGET``/``SOURCE`` argument of
    ``mngr git push``/``mngr git pull``, and the source argument of ``mngr pair``.
    """

    name = "host_location_address"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> HostLocationAddress:
        if isinstance(value, HostLocationAddress):
            return value
        return _convert_with_user_input_error(parse_host_location_address, value, param, ctx)


class AgentNameOrIdParamType(click.ParamType):
    """Click param type for a bare agent name or ID (used by ``mngr rename`` and similar)."""

    name = "agent_name_or_id"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> AgentNameOrId:
        return _convert_with_user_input_error(parse_agent_name_or_id, value, param, ctx)


class HostNameOrIdParamType(click.ParamType):
    """Click param type for a bare host name or ID (used by ``--host`` filter flags)."""

    name = "host_name_or_id"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> HostNameOrId:
        return _convert_with_user_input_error(parse_host_name_or_id, value, param, ctx)


class AgentNameParamType(click.ParamType):
    """Click param type for a bare agent name (rejects IDs).

    Used by ``mngr rename``'s second argument, where the new name must be a
    fresh, human-readable name -- not an existing agent ID.
    """

    name = "agent_name"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> AgentName:
        try:
            return AgentName(value)
        except InvalidName as e:
            raise click.BadParameter(str(e), ctx=ctx, param=param) from e


AGENT_ADDRESS = AgentAddressParamType()
HOST_ADDRESS = HostAddressParamType()
AGENT_OR_HOST_ADDRESS = AgentOrHostAddressParamType()
NEW_AGENT_LOCATION = NewAgentLocationParamType()
HOST_LOCATION_ADDRESS = HostLocationAddressParamType()
AGENT_NAME_OR_ID = AgentNameOrIdParamType()
HOST_NAME_OR_ID = HostNameOrIdParamType()
AGENT_NAME = AgentNameParamType()


def parse_agent_addresses_or_raise(raw: list[str]) -> list[AgentAddress]:
    """Parse a sequence of raw strings into :class:`AgentAddress` values.

    Used by commands whose variadic positional argument supports the stdin
    ``-`` placeholder: those commands keep the positional as raw strings (so
    Click does not try to convert ``-``), expand stdin themselves, and then
    pass the result through this function before calling the API layer.
    """
    try:
        return [parse_agent_address(s) for s in raw]
    except UserInputError as e:
        raise click.BadParameter(str(e)) from e


def parse_agent_or_host_addresses_or_raise(raw: list[str]) -> list[AgentOrHostAddress]:
    """Parse a sequence of raw strings into :class:`AgentOrHostAddress` values.

    Sibling of :func:`parse_agent_addresses_or_raise` for commands whose
    positional argument accepts both agent and host targets (``mngr snapshot
    create/list/destroy``).
    """
    try:
        return [parse_agent_or_host_address(s) for s in raw]
    except UserInputError as e:
        raise click.BadParameter(str(e)) from e
