"""Parsers that produce the typed address shapes shared across mngr.

The four typed shapes -- :class:`HostAddress`, :class:`AgentAddress`,
:class:`NewAgentLocation`, :class:`HostLocationAddress` -- live in
:mod:`imbue.mngr.primitives` so the lower-layer ``config`` package can reference
them in CLI option dataclasses. The parsers are kept here in the api layer
because they raise :class:`UserInputError` (in the ``errors`` module, which
the primitives layer cannot depend on).

Every CLI entry point that takes a host- or agent-shaped argument routes
through these parsers (typically via the Click ParamTypes in
``cli/address_params.py``) so the API layer below operates on parsed types,
not raw strings.

Parsing rules (uniform across all four shapes):

- Dots are deterministic. ``HOST.PROVIDER`` always splits on the single dot;
  host names never contain dots (see :class:`~imbue.mngr.primitives.HostName`).
- The agent / host name parts are typed: an :class:`AgentNameOrId` is parsed
  as an :class:`AgentId` first, then falls back to :class:`AgentName`; same
  for :class:`HostNameOrId`. Inputs that are neither raise :class:`UserInputError`.
"""

from pathlib import Path

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameOrId
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameOrId
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import NewAgentLocation
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


# === Composite parsers ===


@pure
def parse_host_address(s: str) -> HostAddress:
    """Parse a ``[@]HOST[.PROVIDER]`` string into a :class:`HostAddress`.

    The host component is required. Empty input or input that starts with a
    dot raises :class:`UserInputError`. The dot is a deterministic separator:
    real host names do not contain dots in the mngr DSL.

    A leading ``@`` is tolerated for convenience: it is significant only when
    parsing :class:`AgentOrHostAddress` (where it disambiguates host from
    agent), but harmless to allow everywhere else so users can type the same
    string in either context.
    """
    if s.startswith("@"):
        s = s[1:]
    if not s:
        raise UserInputError("Host address cannot be empty")
    host_part, provider_part = _split_host_part(s)
    if not host_part:
        raise UserInputError(f"Host address requires a host name or ID: '{s}'")
    host = parse_host_name_or_id(host_part)
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
def parse_agent_or_host_address(s: str) -> AgentOrHostAddress:
    """Parse a string as either an :class:`AgentAddress` or :class:`HostAddress`.

    Text-only disambiguation rules (no state lookup):

    - A leading ``@`` forces host parsing (``@HOST``, ``@HOST.PROVIDER``).
    - An input that parses as a :class:`HostId` is treated as a host. Without
      this sniff a bare ``host-abc123`` would parse as :class:`AgentName`
      (which permits any :class:`SafeName`) and be misread as an agent.
    - Otherwise the input is tried as an :class:`AgentAddress` first, and
      falls back to :class:`HostAddress` on failure. The fallback path is
      reached for inputs like ``HOST.PROVIDER`` because :class:`AgentName`
      rejects dots.

    Note: a host name shaped like a :class:`SafeName` (no ``host-`` prefix,
    no dots) cannot be targeted by bare text alone -- users must write
    ``@HOST``. This is the deliberate price of state-free parsing.
    """
    if s.startswith("@"):
        return parse_host_address(s)
    try:
        return HostAddress(host=HostId(s))
    except ValueError:
        pass
    try:
        return parse_agent_address(s)
    except UserInputError:
        return parse_host_address(s)


@pure
def parse_new_agent_location(s: str) -> NewAgentLocation:
    """Parse a ``[NAME][@[HOST][.PROVIDER]][:PATH]`` string into a :class:`NewAgentLocation`.

    Empty input parses to all-None (auto-generate everything). The name is
    parsed as :class:`AgentName` only -- IDs are rejected. The host part is
    parsed into the flat ``host_name`` and ``provider_name`` fields directly,
    not via :class:`HostAddress`, so the bare ``.PROVIDER`` form (which means
    "create a new host on this provider") parses cleanly.
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

    if host_part:
        host_str, provider_str = _split_host_part(host_part)
        host_name = parse_host_name_or_id(host_str) if host_str else None
        provider_name = _parse_provider_name(provider_str) if provider_str else None
    else:
        host_name = None
        provider_name = None
    return NewAgentLocation(name=name, host_name=host_name, provider_name=provider_name, path=path)


@pure
def parse_host_location_address(s: str) -> HostLocationAddress:
    """Parse a ``[NAME[@HOST[.PROVIDER]]][:PATH]`` string into a :class:`HostLocationAddress`.

    Used for any CLI argument that designates "a location on some host" --
    sources (``mngr create --from``, ``mngr pair``), the source/destination of
    ``mngr rsync``, and the target of ``mngr git push``/``mngr git pull``.

    Bare paths (starting with ``/``, ``./``, ``~/``, or ``../``) are recognized
    as a convenience. A bare name like ``foo`` always refers to an agent named
    ``foo``, not a directory; use ``:foo`` to mean a relative directory.
    """
    if s.startswith(("/", "./", "~/", "../")):
        return HostLocationAddress(path=Path(s), has_trailing_path_slash=s.endswith("/") and s != "/")

    address_part, path = _split_path_suffix(s)
    if not address_part and path is None:
        return HostLocationAddress()

    agent_str, host_part = _split_at_part(address_part)
    agent = parse_agent_name_or_id(agent_str) if agent_str else None
    host = parse_host_address(host_part) if host_part else None
    return HostLocationAddress(
        agent=agent,
        host=host,
        path=path,
        has_trailing_path_slash=path is not None and s.endswith("/"),
    )


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
