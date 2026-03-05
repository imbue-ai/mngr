from imbue.mng.providers.modal.instance import _build_listing_collection_script
from imbue.mng.providers.modal.instance import _parse_listing_collection_output


def test_build_listing_collection_script_contains_key_sections() -> None:
    script = _build_listing_collection_script("/mng", "mng-")
    assert "UPTIME=" in script
    assert "BTIME=" in script
    assert "LOCK_MTIME=" in script
    assert "SSH_ACTIVITY_MTIME=" in script
    assert "data.json" in script
    assert "MNG_AGENT_START" in script
    assert "MNG_PS_START" in script


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
        "---MNG_DATA_JSON_START---\n"
        '{"host_id": "host-abc", "host_name": "test"}\n'
        "---MNG_DATA_JSON_END---\n"
        "---MNG_PS_START---\n"
        "---MNG_PS_END---\n"
    )
    result = _parse_listing_collection_output(output)
    assert result["certified_data"]["host_id"] == "host-abc"


def test_parse_listing_output_extracts_agent_data() -> None:
    output = (
        "UPTIME=100\n"
        "BTIME=1700000000\n"
        "LOCK_MTIME=\n"
        "SSH_ACTIVITY_MTIME=\n"
        "---MNG_DATA_JSON_START---\n"
        "{}\n"
        "---MNG_DATA_JSON_END---\n"
        "---MNG_PS_START---\n"
        "---MNG_PS_END---\n"
        "---MNG_AGENT_START:agent-123---\n"
        "---MNG_AGENT_DATA_START---\n"
        '{"id": "agent-123", "name": "test-agent", "type": "claude", "command": "claude"}\n'
        "---MNG_AGENT_DATA_END---\n"
        "USER_MTIME=1700000100\n"
        "AGENT_MTIME=1700000200\n"
        "START_MTIME=1700000050\n"
        "TMUX_INFO=0|claude|456\n"
        "ACTIVE=true\n"
        "URL=https://example.com\n"
        "---MNG_AGENT_END---\n"
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
        "---MNG_AGENT_START:agent-bad---\n"
        "---MNG_AGENT_DATA_START---\n"
        "not valid json{{\n"
        "---MNG_AGENT_DATA_END---\n"
        "---MNG_AGENT_END---\n"
    )
    result = _parse_listing_collection_output(output)
    # Agent with malformed JSON should be skipped (no "data" key)
    assert len(result["agents"]) == 0


def test_parse_listing_output_extracts_ps_output() -> None:
    output = "UPTIME=100\n---MNG_PS_START---\n  1   0 init\n100   1 sshd\n---MNG_PS_END---\n"
    result = _parse_listing_collection_output(output)
    assert "init" in result["ps_output"]
    assert "sshd" in result["ps_output"]
