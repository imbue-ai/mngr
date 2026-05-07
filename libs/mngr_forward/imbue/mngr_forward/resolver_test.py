from pathlib import Path

import pytest

from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2


@pytest.fixture
def ssh_info() -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="example.modal.run",
        port=22,
        key_path=Path("/tmp/key"),
    )


def test_resolve_returns_none_for_unknown_agent() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_resolve_service_strategy_returns_none_when_url_unknown() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_resolve_service_strategy_returns_url_when_known() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9100"
    assert target.ssh_info is None


def test_resolve_service_strategy_includes_ssh_info(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    resolver.update_ssh_info(TEST_AGENT_ID_1, ssh_info)
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert target.ssh_info == ssh_info


def test_resolve_port_strategy_returns_fixed_url(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_ssh_info(TEST_AGENT_ID_1, ssh_info)
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8080"
    assert target.ssh_info == ssh_info


def test_update_known_agents_drops_state_for_removed() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:1"})
    resolver.update_known_agents((TEST_AGENT_ID_2,))
    assert resolver.resolve(TEST_AGENT_ID_1) is None
    assert resolver.list_known_agent_ids() == (TEST_AGENT_ID_2,)


def test_remove_known_agent_drops_services() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:1"})
    resolver.remove_known_agent(TEST_AGENT_ID_1)
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_initial_discovery_flag() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.has_completed_initial_discovery() is False
    resolver.update_known_agents(())
    assert resolver.has_completed_initial_discovery() is True
