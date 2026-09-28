from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.providers import list_provider_names_to_load
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.registry import resolve_backend_name


def provider_names_on_backend(mngr_ctx: MngrContext, backend_name: ProviderBackendName) -> tuple[str, ...]:
    """Every enabled provider instance running on ``backend_name`` (a default instance is named after its backend)."""
    matching_names: list[str] = []
    for name in list_provider_names_to_load(mngr_ctx):
        if resolve_backend_name(name, mngr_ctx) == backend_name:
            matching_names.append(str(name))
    return tuple(matching_names)


def resolve_host_on_backend(
    address: HostAddress, mngr_ctx: MngrContext, backend_name: ProviderBackendName
) -> DiscoveredHost:
    """The one host ``address`` names among the providers on ``backend_name``.

    Only that backend's providers are discovered (an unrelated provider that
    cannot be reached must not stop a provider-specific command), unless the
    address names a provider itself. Raises UserInputError when no provider
    on the backend is enabled, or when the address matches no host or more
    than one.
    """
    if address.provider is not None:
        provider_names: tuple[str, ...] = (str(address.provider),)
    else:
        provider_names = provider_names_on_backend(mngr_ctx, backend_name)
        # Discovery reads an empty provider filter as "every provider", not "none".
        if not provider_names:
            raise UserInputError(
                f"No {backend_name} provider is enabled, so there is no {backend_name} host to act on"
            )
    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    return filter_one_host(address, list(outcome.agents_by_host.keys()))
