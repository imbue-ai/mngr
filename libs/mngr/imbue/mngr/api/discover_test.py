import pytest

from imbue.mngr.api.discover import DiscoveryOutcome
from imbue.mngr.api.discover import Unreachable
from imbue.mngr.api.discover import filter_discovery_outcome_to_address_host
from imbue.mngr.api.find import _match_one_agent_in_outcome
from imbue.mngr.errors import AgentNameNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import allow_warnings


@pytest.mark.parametrize(
    ("provider", "expected_error"),
    [
        (ProviderInstanceName("local"), AgentNameNotFoundError),
        (None, ProviderUnavailableError),
    ],
)
def test_a_shared_outcome_blames_another_providers_outage_only_on_an_address_that_could_live_there(
    provider: ProviderInstanceName | None,
    expected_error: type[MngrError],
) -> None:
    """An address filtered out of a pass shared with other addresses fails as a lookup of it alone would.

    Alone, ``missing@box.local`` asks only the local provider, so a miss is not
    found; ``missing@box`` asks every provider, so a docker outage leaves it
    unknown. The shared pass here reached docker for some other address.
    """
    local_host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("box"), provider_name=ProviderInstanceName("local")
    )
    shared_outcome = DiscoveryOutcome(
        agents_by_host={local_host: []},
        results_by_provider={
            ProviderInstanceName("docker"): Unreachable(
                provider_name=ProviderInstanceName("docker"),
                error_type_name="ProviderUnavailableError",
                error_message="docker is down",
            )
        },
    )
    address = AgentAddress(agent=AgentName("missing"), host=HostAddress(host=HostName("box"), provider=provider))

    with allow_warnings(match="Agent lookup failed"), pytest.raises(expected_error):
        _match_one_agent_in_outcome(address, filter_discovery_outcome_to_address_host(shared_outcome, address))
