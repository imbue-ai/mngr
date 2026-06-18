from datetime import datetime
from datetime import timezone

from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_modal.instance import ModalProviderInstance


def _make_host_details() -> HostDetails:
    return HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal-test"),
    )


def test_build_single_agent_details_returns_agent_with_correct_state(
    testing_provider: ModalProviderInstance,
) -> None:
    """_build_single_agent_details sets lifecycle state from tmux info and ps output."""
    agent_id = str(AgentId.generate())
    agent_raw: dict = {
        "data": {
            "id": agent_id,
            "name": "test-agent",
            "type": "unknown-type",
            "command": "my-agent",
            "create_time": datetime.now(timezone.utc).isoformat(),
        },
        "tmux_info": "0|bash|100",
        "is_active": False,
    }
    result = testing_provider._build_single_agent_details(
        agent_raw=agent_raw,
        host_details=_make_host_details(),
        ssh_activity=None,
        ps_output="",
        idle_timeout_seconds=300,
        activity_sources=(ActivitySource.USER,),
        idle_mode=IdleMode.USER,
    )
    assert result is not None
    # pane shows bash shell, expected process is "my-agent" (not found) -> DONE
    assert result.state == AgentLifecycleState.DONE


def test_build_single_agent_details_returns_none_for_missing_id(
    testing_provider: ModalProviderInstance,
) -> None:
    """_build_single_agent_details returns None when agent data has no id."""
    agent_raw: dict = {"data": {"name": "no-id-agent"}}
    result = testing_provider._build_single_agent_details(
        agent_raw=agent_raw,
        host_details=_make_host_details(),
        ssh_activity=None,
        ps_output="",
        idle_timeout_seconds=300,
        activity_sources=(ActivitySource.USER,),
        idle_mode=IdleMode.USER,
    )
    assert result is None
