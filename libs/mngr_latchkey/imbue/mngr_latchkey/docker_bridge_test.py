import pytest

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr_latchkey.docker_bridge import DockerBridgeAddressError
from imbue.mngr_latchkey.docker_bridge import resolve_docker_bridge_address
from imbue.mngr_latchkey.remote._machine import MACHINE_GENERATION_FILE
from imbue.mngr_latchkey.remote._machine import OUTCOME_MACHINE_NEWER_MARKER
from imbue.mngr_latchkey.remote._machine import machine_generation_check
from imbue.mngr_latchkey.remote.errors import MachineNewerError
from imbue.mngr_latchkey.remote.mock_outer_host_test import scripted_outer


def test_resolve_docker_bridge_address_returns_the_probed_address() -> None:
    outer, stub = scripted_outer(docker_bridge_address="172.18.0.1")
    assert resolve_docker_bridge_address(outer) == "172.18.0.1"
    assert "addr show docker0" in stub.recorded[-1].command


def test_resolve_docker_bridge_address_raises_when_the_probe_fails() -> None:
    outer, _ = scripted_outer(
        result_by_substring={
            "addr show docker0": CommandResult(stdout="", stderr="ip: command not found", success=False)
        }
    )
    with pytest.raises(DockerBridgeAddressError, match="ip: command not found"):
        resolve_docker_bridge_address(outer)


@pytest.mark.parametrize("answer", ["", "0.0.0.0", "::", "[::]", "*"])
def test_resolve_docker_bridge_address_refuses_a_wildcard_answer(answer: str) -> None:
    # Failing closed: a wildcard is what a caller would end up binding if it
    # took the answer at face value, and on a VPS that is the public interface.
    outer, _ = scripted_outer(docker_bridge_address=answer)
    with pytest.raises(DockerBridgeAddressError, match="Could not resolve a docker bridge address"):
        resolve_docker_bridge_address(outer)


def test_resolve_docker_bridge_address_stops_at_a_machine_of_a_newer_generation() -> None:
    """The probe is a pass's first command, so it carries the check that finds a newer machine before any upload."""
    outer, stub = scripted_outer(
        result_by_substring={
            "addr show docker0": CommandResult(stdout=f"{OUTCOME_MACHINE_NEWER_MARKER}\n", stderr="", success=True)
        }
    )

    with pytest.raises(MachineNewerError):
        resolve_docker_bridge_address(outer)

    command = stub.recorded[-1].command
    check = machine_generation_check(MACHINE_GENERATION_FILE, None)
    assert command.startswith(check + "\n")
    assert command.index(check) < command.index("addr show docker0")
