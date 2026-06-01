"""Unit tests for the kanpan plugin's muted field generators.

The online generator (`_muted_online_field`) is exercised end-to-end by the
acceptance tests (a real local agent flows through `list_agents`). The offline
generator (`_muted_offline_field`) reads the persisted `plugin.kanpan.muted`
bit straight off a `DiscoveredAgent`'s certified data, so it is covered here at
the unit level.
"""

from typing import Any

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr_kanpan.plugin import _muted_offline_field

# The offline generator ignores its host_details argument; any valid HostDetails works.
_HOST_DETAILS = make_test_agent_details().host


def _offline_ref(certified_data: dict[str, Any]) -> DiscoveredAgent:
    """Build a DiscoveredAgent with the given certified data for offline-generator tests."""
    return DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("offline-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data=certified_data,
    )


def test_muted_offline_field_true_when_muted() -> None:
    ref = _offline_ref({"plugin": {"kanpan": {"muted": True}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is True


def test_muted_offline_field_none_when_explicitly_unmuted() -> None:
    # The generator is sparse: it returns None (omitting the field) rather than
    # False, so the board reads it back as unmuted via its `.get(..., False)`.
    ref = _offline_ref({"plugin": {"kanpan": {"muted": False}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None


def test_muted_offline_field_none_when_certified_data_empty() -> None:
    assert _muted_offline_field(_offline_ref({}), _HOST_DETAILS) is None


def test_muted_offline_field_none_when_no_kanpan_plugin() -> None:
    ref = _offline_ref({"plugin": {}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None


def test_muted_offline_field_none_when_no_muted_key() -> None:
    ref = _offline_ref({"plugin": {"kanpan": {}}})
    assert _muted_offline_field(ref, _HOST_DETAILS) is None
