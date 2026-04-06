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
from imbue.mngr_modal.instance import _build_listing_collection_script
from imbue.mngr_modal.instance import _parse_listing_collection_output


def test_build_listing_collection_script_contains_key_sections() -> None:
    script = _build_listing_collection_script("/mngr", "mngr-")
    assert "UPTIME=" in script
    assert "BTIME=" in script
    assert "LOCK_MTIME=" in script
    assert "SSH_ACTIVITY_MTIME=" in script
    assert "data.json" in script
    assert "MNGR_AGENT_START" in script
    assert "MNGR_PS_START" in script


def test_parse_listing_output_extracts_uptime() -> None:
    output = "UPTIME=123.45\nBTIME=\nLOCK_MTIME=\nSSH_ACTIVITY_MTIME=\n"
    result = _parse_listing_collection_output(output)
    assert result["uptime_seconds"] == 123.45


def test_parse_listing_output_extracts_btime() -> None:
    output = "UPTIME=\nBTIME=1700000000\nLOCK_MTIME=\nSSH_ACTIVITY_MTIME=\n"
    result = _parse_listing_collection_output(output)
    assert result["btime"] == 1700000000


def test_parse_listing_output_handles_empty_values() -> None:
    output = "UPTIME=\nBTIME=\nLOCK_MTIME=\nSSH_ACTIVITY_MTIME=\n"
    result = _parse_listing_collection_output(output)
    assert result.get("uptime_seconds") is None
    assert result.get("btime") is None
    assert result.get("lock_mtime") is None
    assert result.get("ssh_activity_mtime") is None


def test_parse_listing_output_extracts_certified_data() -> None:
    output = (
        "UPTIME=100\n"
        "BTIME=1700000000\n"
        "LOCK_MTIME=\n"
        "SSH_ACTIVITY_MTIME=\n"
        "---MNGR_DATA_JSON_START---\n"
        '{"host_id": "host-abc", "host_name": "test"}\n'
        "---MNGR_DATA_JSON_END---\n"
        "---MNGR_PS_START---\n"
        "---MNGR_PS_END---\n"
    )
    result = _parse_listing_collection_output(output)
    assert result["certified_data"]["host_id"] == "host-abc"


def test_parse_listing_output_extracts_agent_data() -> None:
    output = (
        "UPTIME=100\n"
        "BTIME=1700000000\n"
        "LOCK_MTIME=\n"
        "SSH_ACTIVITY_MTIME=\n"
        "---MNGR_DATA_JSON_START---\n"
        "{}\n"
        "---MNGR_DATA_JSON_END---\n"
        "---MNGR_PS_START---\n"
        "---MNGR_PS_END---\n"
        "---MNGR_AGENT_START:agent-123---\n"
        "---MNGR_AGENT_DATA_START---\n"
        '{"id": "agent-123", "name": "test-agent", "type": "claude", "command": "claude"}\n'
        "---MNGR_AGENT_DATA_END---\n"
        "USER_MTIME=1700000100\n"
        "AGENT_MTIME=1700000200\n"
        "START_MTIME=1700000050\n"
        "TMUX_INFO=0|claude|456\n"
        "ACTIVE=true\n"
        "URL=https://example.com\n"
        "---MNGR_AGENT_END---\n"
    )
    result = _parse_listing_collection_output(output)
    agents = result["agents"]
    assert len(agents) == 1
    agent = agents[0]
    assert agent["data"]["id"] == "agent-123"
    assert agent["user_activity_mtime"] == 1700000100
    assert agent["agent_activity_mtime"] == 1700000200
    assert agent["start_activity_mtime"] == 1700000050
    assert agent["tmux_info"] == "0|claude|456"
    assert agent["is_active"] is True
    assert agent["url"] == "https://example.com"


def test_parse_listing_output_handles_malformed_agent_json() -> None:
    output = (
        "UPTIME=100\n"
        "---MNGR_AGENT_START:agent-bad---\n"
        "---MNGR_AGENT_DATA_START---\n"
        "not valid json{{\n"
        "---MNGR_AGENT_DATA_END---\n"
        "---MNGR_AGENT_END---\n"
    )
    result = _parse_listing_collection_output(output)
    # Agent with malformed JSON should be skipped (no "data" key)
    assert len(result["agents"]) == 0


def test_parse_listing_output_extracts_ps_output() -> None:
    output = "UPTIME=100\n---MNGR_PS_START---\n  1   0 init\n100   1 sshd\n---MNGR_PS_END---\n"
    result = _parse_listing_collection_output(output)
    assert "init" in result["ps_output"]
    assert "sshd" in result["ps_output"]


# =========================================================================
# _build_single_agent_details tests
# =========================================================================


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
