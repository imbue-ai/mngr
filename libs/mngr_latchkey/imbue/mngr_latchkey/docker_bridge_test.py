import pytest

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr_latchkey.docker_bridge import DockerBridgeAddressError
from imbue.mngr_latchkey.docker_bridge import resolve_docker_bridge_address
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_stub
from imbue.mngr_latchkey.remote.mock_outer_host_test import stub_outer


def test_resolve_docker_bridge_address_returns_the_probed_address() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    as_stub(outer).docker_bridge_address = "172.18.0.1"
    assert resolve_docker_bridge_address(outer) == "172.18.0.1"
    command = as_stub(outer).recorded[-1].command
    assert "addr show docker0" in command


def test_resolve_docker_bridge_address_raises_when_the_probe_fails() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="ip: command not found", success=False))
    with pytest.raises(DockerBridgeAddressError, match="ip: command not found"):
        resolve_docker_bridge_address(outer)


@pytest.mark.parametrize("answer", ["", "0.0.0.0", "::", "[::]", "*"])
def test_resolve_docker_bridge_address_refuses_a_wildcard_answer(answer: str) -> None:
    # Failing closed: a wildcard is what a caller would end up binding if it
    # took the answer at face value, and on a VPS that is the public interface.
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    as_stub(outer).docker_bridge_address = answer
    with pytest.raises(DockerBridgeAddressError, match="Could not resolve a docker bridge address"):
        resolve_docker_bridge_address(outer)
