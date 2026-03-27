from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng.api.find import AgentMatch
from imbue.mng.api.find import find_agents_by_identifiers_or_state
from imbue.mng.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName


class AgentAddress(FrozenModel):
    """Parsed agent address from [NAME][@[HOST][.PROVIDER]] format.

    Used to specify an agent and optionally its target host and provider in a single
    positional argument. Examples:
      - "foo" -> agent named "foo", local host
      - "foo@myhost" -> agent "foo" on existing host "myhost"
      - "foo@myhost.modal" -> agent "foo" on existing host "myhost" (on modal provider)
      - "foo@.modal" -> agent "foo" on a new host with auto-generated name on modal
      - "@myhost.modal" -> auto-named agent on existing host "myhost" (on modal provider)
    """

    agent_name: AgentName | None = None
    host_name: HostName | None = None
    provider_name: ProviderInstanceName | None = None

    @property
    def has_host_component(self) -> bool:
        """True when any host or provider info was specified in the address."""
        return self.host_name is not None or self.provider_name is not None


@pure
def parse_agent_address(address_str: str) -> AgentAddress:
    """Parse an agent address string into its components.

    Format: [AGENT_NAME][@[HOST_NAME][.PROVIDER_NAME]]

    The host part (after @) may contain at most one dot separating the host name
    from the provider name. Additional dots are not allowed.

    Examples:
      - "" -> everything None (auto-generate name, local host)
      - "foo" -> agent_name="foo"
      - "foo@myhost" -> agent_name="foo", host_name="myhost"
      - "foo@myhost.modal" -> agent_name="foo", host_name="myhost", provider_name="modal"
      - "foo@.modal" -> agent_name="foo", provider_name="modal" (implies new host)
      - "@myhost.modal" -> host_name="myhost", provider_name="modal" (auto-generate name)
    """
    if not address_str:
        return AgentAddress()

    if "@" not in address_str:
        # Simple agent name with no host component
        return AgentAddress(agent_name=AgentName(address_str))

    agent_part, host_part = address_str.split("@", 1)
    agent_name = AgentName(agent_part) if agent_part else None

    if not host_part:
        # "foo@" -> just agent name, no host component
        return AgentAddress(agent_name=agent_name)

    dot_count = host_part.count(".")
    if dot_count > 1:
        raise UserInputError(
            f"Invalid agent address: host part '{host_part}' contains more than one dot. "
            "Expected format: [NAME][@[HOST][.PROVIDER]]"
        )

    if dot_count == 1:
        host_str, provider_str = host_part.split(".", 1)
        host_name = HostName(host_str) if host_str else None
        provider_name = ProviderInstanceName(provider_str) if provider_str else None
    else:
        host_name = HostName(host_part)
        provider_name = None

    return AgentAddress(agent_name=agent_name, host_name=host_name, provider_name=provider_name)


@pure
def parse_identifier_as_address(raw: str) -> tuple[str, AgentAddress]:
    """Parse a raw identifier string as an agent address.

    Returns (identifier_str, address) where identifier_str is the agent name or ID
    portion to use for matching. For plain strings without '@', the raw string is
    returned unchanged (preserving backward compatibility with agent IDs).
    """
    if "@" not in raw:
        return raw, AgentAddress(agent_name=AgentName(raw))

    address = parse_agent_address(raw)
    # Use the agent_name as the identifier string, or the raw string if no name part
    identifier = str(address.agent_name) if address.agent_name is not None else raw
    return identifier, address


@pure
def _address_matches_host(address: AgentAddress, host_ref: DiscoveredHost) -> bool:
    """Check if a discovered host satisfies the host/provider constraints of an address."""
    if address.host_name is not None and host_ref.host_name != address.host_name:
        return False
    if address.provider_name is not None and host_ref.provider_name != address.provider_name:
        return False
    return True


@pure
def _address_matches_agent_match(address: AgentAddress, match: AgentMatch) -> bool:
    """Check if an AgentMatch satisfies the host/provider constraints of an address."""
    if address.host_name is not None and match.host_name != address.host_name:
        return False
    if address.provider_name is not None and match.provider_name != address.provider_name:
        return False
    return True


@pure
def filter_agents_by_host_constraint(
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    address: AgentAddress,
) -> dict[DiscoveredHost, Sequence[DiscoveredAgent]]:
    """Filter agents_by_host to only include hosts matching the address constraints.

    If the address has no host component, returns the original mapping unchanged.
    """
    if not address.has_host_component:
        return dict(agents_by_host)

    return {
        host_ref: agent_refs
        for host_ref, agent_refs in agents_by_host.items()
        if _address_matches_host(address, host_ref)
    }


def find_agents_by_addresses(
    raw_identifiers: Sequence[str],
    filter_all: bool,
    target_state: AgentLifecycleState | None,
    mng_ctx: MngContext,
    include_destroyed: bool = False,
) -> list[AgentMatch]:
    """Find agents by identifiers that may contain agent addresses.

    Like find_agents_by_identifiers_or_state but supports agent addresses
    in the format [NAME][@[HOST][.PROVIDER]].

    For identifiers without host/provider components, behaves identically to
    find_agents_by_identifiers_or_state. For identifiers with host/provider
    components, post-filters the results to only include agents on matching hosts.
    """
    # Parse all identifiers
    parsed: list[tuple[str, AgentAddress]] = [parse_identifier_as_address(raw) for raw in raw_identifiers]

    # Extract plain identifier strings (agent names/IDs)
    plain_identifiers = [ident for ident, _ in parsed]

    # Delegate to the existing find function
    matches = find_agents_by_identifiers_or_state(
        agent_identifiers=plain_identifiers,
        filter_all=filter_all,
        target_state=target_state,
        mng_ctx=mng_ctx,
        include_destroyed=include_destroyed,
    )

    return _post_filter_matches_by_addresses(raw_identifiers, parsed, matches)


@pure
def _post_filter_matches_by_addresses(
    raw_identifiers: Sequence[str],
    parsed: Sequence[tuple[str, AgentAddress]],
    matches: Sequence[AgentMatch],
) -> list[AgentMatch]:
    """Post-filter agent matches by host/provider constraints from parsed addresses.

    For identifiers without host/provider components, matches pass through unchanged.
    For identifiers with host/provider components, only matches on the specified
    host/provider are kept. Raises AgentNotFoundError if a constrained identifier
    has no matching agents after filtering.
    """
    # Check if any identifiers have host constraints
    has_host_constraints = any(addr.has_host_component for _, addr in parsed)

    # If no host constraints, return as-is
    if not has_host_constraints:
        return list(matches)

    # Build a mapping from agent name -> list of addresses with host constraints
    name_to_addresses: dict[str, list[AgentAddress]] = {}
    for ident, addr in parsed:
        if addr.has_host_component:
            name_to_addresses.setdefault(ident, []).append(addr)

    filtered: list[AgentMatch] = []
    for match in matches:
        agent_name_str = str(match.agent_name)
        agent_id_str = str(match.agent_id)

        # Check if this match's name or ID has associated address constraints
        addresses_for_match = name_to_addresses.get(agent_name_str) or name_to_addresses.get(agent_id_str)
        # Include if there are no constraints, or if at least one constraint is satisfied
        if addresses_for_match is None or any(
            _address_matches_agent_match(addr, match) for addr in addresses_for_match
        ):
            filtered.append(match)

    # Check that all constrained identifiers have at least one match
    for raw, (ident, addr) in zip(raw_identifiers, parsed, strict=True):
        if not addr.has_host_component:
            continue
        has_match = any(str(m.agent_name) == ident or str(m.agent_id) == ident for m in filtered)
        if not has_match:
            raise AgentNotFoundError(f"No agent found matching address: {raw}")

    return filtered


def find_agent_by_address(
    agent_str: str,
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    mng_ctx: MngContext,
    command_name: str,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Find an agent by address string, supporting host/provider disambiguation.

    Parses the address, filters the agents_by_host mapping by host/provider
    constraints, then delegates to find_and_maybe_start_agent_by_name_or_id.
    """
    identifier, address = parse_identifier_as_address(agent_str)

    filtered_agents_by_host = filter_agents_by_host_constraint(agents_by_host, address)

    if address.has_host_component and not filtered_agents_by_host:
        host_desc = ""
        if address.host_name is not None:
            host_desc += f" host '{address.host_name}'"
        if address.provider_name is not None:
            host_desc += f" provider '{address.provider_name}'"
        raise UserInputError(f"No hosts found matching{host_desc}")

    return find_and_maybe_start_agent_by_name_or_id(
        identifier,
        filtered_agents_by_host,
        mng_ctx,
        command_name,
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
    )
