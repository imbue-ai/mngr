"""Parsed address types and parsers shared across mngr.

This module defines the four kinds of address strings that appear in mngr CLI
arguments and configuration, along with the parsers that produce them. Every
CLI entry point that takes a host- or agent-shaped argument routes through
these parsers (typically via the Click ParamTypes in ``cli/address_params.py``)
so the API layer below operates on parsed types, not raw strings.

The four address shapes are:

- :class:`HostAddress` — ``HOST[.PROVIDER]`` or ``.PROVIDER``. References a host
  (or, in the bare ``.PROVIDER`` form, hints at a new host on a provider). At
  least one of host/provider is set.
- :class:`AgentAddress` — ``NAME[@HOST[.PROVIDER]]``. A required agent
  name-or-id plus an optional host address.
- :class:`NewAgentLocation` — ``[NAME][@[HOST][.PROVIDER]][:PATH]``. The
  positional argument of ``mngr create``: name is optional (auto-generated when
  omitted) and a path can be appended for the agent's work directory.
- :class:`SourceLocation` — ``[NAME[@HOST[.PROVIDER]]][:PATH]`` or a bare path.
  The argument of ``mngr create --from``: any combination of agent / host /
  path is allowed (a fully empty location resolves to ``$cwd`` on the local
  host downstream).

Parsing rules (uniform across all four shapes):

- Dots are deterministic. ``HOST.PROVIDER`` always splits on the single dot;
  host names never contain dots (see :class:`~imbue.mngr.primitives.HostName`).
- The agent / host name parts are typed: an :class:`AgentNameOrId` is parsed
  as an :class:`AgentId` first, then falls back to :class:`AgentName`; same
  for :class:`HostNameOrId`. Inputs that are neither raise :class:`UserInputError`.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameOrId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameOrId
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import ProviderInstanceName

# === Atomic parsers ===


@pure
def parse_agent_name_or_id(s: str) -> AgentNameOrId:
    """Parse a string as an :class:`AgentId` if it has the right shape, else as :class:`AgentName`.

    Raises :class:`UserInputError` if the string is neither a valid agent ID nor
    a valid agent name.
    """
    try:
        return AgentId(s)
    except ValueError:
        pass
    try:
        return AgentName(s)
    except InvalidName as e:
        raise UserInputError(f"Not a valid agent name or ID: '{s}' ({e})") from e


@pure
def parse_host_name_or_id(s: str) -> HostNameOrId:
    """Parse a string as a :class:`HostId` if it has the right shape, else as :class:`HostName`.

    Raises :class:`UserInputError` if the string is neither a valid host ID nor
    a valid host name.
    """
    try:
        return HostId(s)
    except ValueError:
        pass
    try:
        return HostName(s)
    except InvalidName as e:
        raise UserInputError(f"Not a valid host name or ID: '{s}' ({e})") from e


@pure
def _parse_provider_name(s: str) -> ProviderInstanceName:
    """Parse a string as a :class:`ProviderInstanceName`, raising :class:`UserInputError` on bad input."""
    try:
        return ProviderInstanceName(s)
    except InvalidName as e:
        raise UserInputError(f"Not a valid provider name: '{s}' ({e})") from e


# === Address types ===


class HostAddress(FrozenModel):
    """A parsed ``HOST[.PROVIDER]`` (or bare ``.PROVIDER``) string.

    At least one of ``host`` or ``provider`` is set. The bare ``.PROVIDER``
    form (host omitted) is only meaningful in contexts that allow creating a
    new host -- for example, ``mngr create NAME@.modal``. Most other contexts
    require ``host`` to be set; they should validate that explicitly.
    """

    host: HostNameOrId | None = Field(default=None, description="Host name or ID")
    provider: ProviderInstanceName | None = Field(
        default=None, description="Provider instance name (the ``.PROVIDER`` qualifier)"
    )

    @model_validator(mode="after")
    def _at_least_one_component(self) -> "HostAddress":
        if self.host is None and self.provider is None:
            raise UserInputError("Host address must specify at least a host or a provider")
        return self

    def __str__(self) -> str:
        if self.host is not None and self.provider is not None:
            return f"{self.host}.{self.provider}"
        if self.host is not None:
            return str(self.host)
        return f".{self.provider}"


class AgentAddress(FrozenModel):
    """A parsed ``NAME[@HOST[.PROVIDER]]`` string.

    The agent component is required; without it, this is not an agent address.
    Use :class:`HostAddress` for ``@HOST.PROVIDER`` (no agent) or
    :class:`SourceLocation` for ``--from`` syntax.
    """

    agent: AgentNameOrId = Field(description="Agent name or ID (required)")
    host: HostAddress | None = Field(default=None, description="Optional host disambiguator")

    def __str__(self) -> str:
        if self.host is None:
            return str(self.agent)
        return f"{self.agent}@{self.host}"


class NewAgentLocation(FrozenModel):
    """A parsed ``[NAME][@[HOST][.PROVIDER]][:PATH]`` string.

    Used as the positional argument of ``mngr create``. The agent name is
    optional (omitted means "auto-generate"); the host part can refer to an
    existing host (``HOST[.PROVIDER]``) or hint at creating a new host on a
    provider (bare ``.PROVIDER``). The trailing ``:PATH`` overrides the agent's
    default work-directory location.

    Note that ``name`` is :class:`AgentName`, not :class:`AgentNameOrId` --
    the agent doesn't yet exist when ``mngr create`` runs, so referring to it
    by ID is meaningless.
    """

    name: AgentName | None = Field(default=None, description="Optional explicit agent name")
    host: HostAddress | None = Field(default=None, description="Optional host disambiguator or new-host hint")
    path: Path | None = Field(default=None, description="Optional explicit work-directory path inside the host")


class SourceLocation(FrozenModel):
    """A parsed ``--from`` argument: ``[NAME[@HOST[.PROVIDER]]][:PATH]`` or a bare path.

    Every component is optional. The four meaningful shapes (in addition to a
    bare path string) are:

    - ``AGENT`` -> agent's host + agent's work_dir
    - ``AGENT:PATH`` -> agent's host + explicit ``PATH``
    - ``@HOST[.PROVIDER]:PATH`` -> explicit host + ``PATH``
    - ``:PATH`` -> local path

    A bare path string starting with ``/``, ``./``, ``~/``, or ``../`` is also
    parsed directly into ``path`` as a convenience.
    """

    agent: AgentNameOrId | None = Field(default=None, description="Optional source agent name or ID")
    host: HostAddress | None = Field(default=None, description="Optional source host")
    path: Path | None = Field(default=None, description="Optional source path")


# === Composite parsers ===


@pure
def parse_host_address(s: str) -> HostAddress:
    """Parse a ``HOST[.PROVIDER]`` (or bare ``.PROVIDER``) string into a :class:`HostAddress`.

    Empty input raises :class:`UserInputError`. The dot is a deterministic
    separator: real host names do not contain dots in the mngr DSL.
    """
    if not s:
        raise UserInputError("Host address cannot be empty")
    host_part, provider_part = _split_host_part(s)
    host = parse_host_name_or_id(host_part) if host_part else None
    provider = _parse_provider_name(provider_part) if provider_part else None
    return HostAddress(host=host, provider=provider)


@pure
def parse_agent_address(s: str) -> AgentAddress:
    """Parse a ``NAME[@HOST[.PROVIDER]]`` string into an :class:`AgentAddress`.

    The name component is required. Empty input or a leading ``@`` raises
    :class:`UserInputError`.
    """
    if not s:
        raise UserInputError("Agent address cannot be empty")
    name_part, host_part = _split_at_part(s)
    if not name_part:
        raise UserInputError(f"Agent address requires a name or ID: '{s}'")
    agent = parse_agent_name_or_id(name_part)
    host = parse_host_address(host_part) if host_part is not None else None
    return AgentAddress(agent=agent, host=host)


@pure
def parse_new_agent_location(s: str) -> NewAgentLocation:
    """Parse a ``[NAME][@[HOST][.PROVIDER]][:PATH]`` string into a :class:`NewAgentLocation`.

    Empty input parses to all-None (auto-generate everything). The name is
    parsed as :class:`AgentName` only -- IDs are rejected.
    """
    address_part, path = _split_path_suffix(s)
    name_str, host_part = _split_at_part(address_part)

    if name_str is None or not name_str:
        name: AgentName | None = None
    else:
        try:
            name = AgentName(name_str)
        except InvalidName as e:
            raise UserInputError(f"Not a valid agent name: '{name_str}' ({e})") from e

    host = parse_host_address(host_part) if host_part else None
    return NewAgentLocation(name=name, host=host, path=path)


@pure
def parse_source_location(s: str) -> SourceLocation:
    """Parse a ``--from``/``--source`` string into a :class:`SourceLocation`.

    Bare paths (starting with ``/``, ``./``, ``~/``, or ``../``) are recognized
    as a convenience. A bare name like ``foo`` always refers to an agent named
    ``foo``, not a directory; use ``:foo`` to mean a relative directory.
    """
    if s.startswith(("/", "./", "~/", "../")):
        return SourceLocation(path=Path(s))

    address_part, path = _split_path_suffix(s)
    if not address_part and path is None:
        return SourceLocation()

    agent_str, host_part = _split_at_part(address_part)
    agent = parse_agent_name_or_id(agent_str) if agent_str else None
    host = parse_host_address(host_part) if host_part else None
    return SourceLocation(agent=agent, host=host, path=path)


# === Internal split helpers ===


@pure
def _split_host_part(s: str) -> tuple[str, str]:
    """Split a ``HOST[.PROVIDER]`` string on its single dot.

    Returns ``(host_str, provider_str)``; either may be empty (representing the
    component absent), but never both. Raises :class:`UserInputError` on more
    than one dot.
    """
    dot_count = s.count(".")
    if dot_count > 1:
        raise UserInputError(
            f"Invalid host address '{s}': contains more than one dot. Expected format: HOST[.PROVIDER]"
        )
    if dot_count == 0:
        return (s, "")
    host_str, provider_str = s.split(".", 1)
    return (host_str, provider_str)


@pure
def _split_at_part(s: str) -> tuple[str | None, str | None]:
    """Split a ``[NAME][@[HOST][.PROVIDER]]`` string on its single ``@``.

    Returns ``(name_str, host_part)`` where ``host_part`` is ``None`` if there
    was no ``@``, an empty string if there was a trailing ``@`` with nothing
    after it, or the substring after ``@`` otherwise. Likewise ``name_str`` is
    ``None`` for a bare ``@HOST`` form.
    """
    if "@" not in s:
        return (s or None, None)
    name_part, host_part = s.split("@", 1)
    return (name_part or None, host_part)


@pure
def _split_path_suffix(s: str) -> tuple[str, Path | None]:
    """Split a ``ADDRESS[:PATH]`` string on its first ``:``.

    Returns ``(address_part, path)`` where ``path`` is ``None`` if there was no
    ``:`` or only a trailing one with nothing after.
    """
    if ":" not in s:
        return (s, None)
    address_part, path_str = s.split(":", 1)
    return (address_part, Path(path_str) if path_str else None)


# === Convenience helpers used by callers ===


@pure
def host_addresses_match(a: HostAddress, b: HostAddress) -> bool:
    """True if every component set on ``a`` matches the corresponding component on ``b``.

    Used to filter discovered hosts by an address constraint: an address with
    only ``provider=docker`` matches every docker host, regardless of name.
    """
    if a.host is not None and a.host != b.host:
        return False
    if a.provider is not None and a.provider != b.provider:
        return False
    return True


@pure
def collect_required_provider_names(
    addresses: Sequence[AgentAddress],
) -> tuple[ProviderInstanceName, ...] | None:
    """Return the set of provider names a discovery call can be restricted to.

    If every address has a provider set, returns the deduped tuple. If any
    address omits the provider, returns ``None`` (meaning: all providers must
    be queried).
    """
    providers: set[ProviderInstanceName] = set()
    for addr in addresses:
        if addr.host is None or addr.host.provider is None:
            return None
        providers.add(addr.host.provider)
    if not providers:
        return None
    return tuple(sorted(providers))
