"""Click ``ParamType`` adapters that parse mngr address strings into typed values.

Each ParamType wraps one of the parsers in :mod:`imbue.mngr.api.address_parsers`.
Click invokes these during argument parsing, so command bodies receive typed
addresses (``AgentAddress``, ``HostAddress``, ``NewAgentLocation``,
``HostedLocation``) rather than raw strings.

Use the module-level singletons (``AGENT_ADDRESS``, ``HOST_ADDRESS``, ...) as
the ``type=`` value on a ``@click.option`` or ``@click.argument`` decorator.
"""

from typing import Any

import click

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.address_parsers import parse_agent_name_or_id
from imbue.mngr.api.address_parsers import parse_host_address
from imbue.mngr.api.address_parsers import parse_host_name_or_id
from imbue.mngr.api.address_parsers import parse_hosted_location
from imbue.mngr.api.address_parsers import parse_new_agent_location
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameOrId
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostNameOrId
from imbue.mngr.primitives import HostedLocation
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


class NewAgentLocationParamType(click.ParamType):
    """Click param type for ``mngr create``'s ``[NAME][@HOST[.PROVIDER]][:PATH]`` argument."""

    name = "new_agent_location"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> NewAgentLocation:
        if isinstance(value, NewAgentLocation):
            return value
        return _convert_with_user_input_error(parse_new_agent_location, value, param, ctx)


class HostedLocationParamType(click.ParamType):
    """Click param type for ``[NAME[@HOST[.PROVIDER]]][:PATH]`` source/target arguments.

    Used by commands that designate "a location on any host" -- e.g. the
    ``--from`` argument of ``mngr create``, the ``TARGET``/``SOURCE`` argument
    of ``mngr push``/``mngr pull``, and the source argument of ``mngr pair``.
    """

    name = "hosted_location"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> HostedLocation:
        if isinstance(value, HostedLocation):
            return value
        return _convert_with_user_input_error(parse_hosted_location, value, param, ctx)


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
NEW_AGENT_LOCATION = NewAgentLocationParamType()
HOSTED_LOCATION = HostedLocationParamType()
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
