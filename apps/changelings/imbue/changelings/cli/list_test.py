import json
from typing import Any

from click.testing import CliRunner

from imbue.changelings.cli.list import _DEFAULT_DISPLAY_FIELDS
from imbue.changelings.cli.list import _HEADER_LABELS
from imbue.changelings.cli.list import _build_table
from imbue.changelings.cli.list import _emit_human_output
from imbue.changelings.cli.list import _emit_json_output
from imbue.changelings.cli.list import _get_field_value
from imbue.changelings.main import cli
from imbue.changelings.testing import capture_loguru_messages

_RUNNER = CliRunner()


def _make_agent_dict(
    agent_id: str,
    name: str = "test-agent",
    state: str = "RUNNING",
    host_state: str = "RUNNING",
    provider: str = "local",
) -> dict[str, Any]:
    """Create a mock mng agent dict."""
    return {
        "id": agent_id,
        "name": name,
        "state": state,
        "host": {
            "name": "@local",
            "state": host_state,
            "provider_name": provider,
        },
    }


# --- _get_field_value tests ---


def test_get_field_value_simple() -> None:
    agent = {"name": "my-agent", "state": "RUNNING"}

    assert _get_field_value(agent, "name") == "my-agent"
    assert _get_field_value(agent, "state") == "RUNNING"


def test_get_field_value_nested() -> None:
    agent = {"host": {"name": "@local", "state": "RUNNING"}}

    assert _get_field_value(agent, "host.name") == "@local"
    assert _get_field_value(agent, "host.state") == "RUNNING"


def test_get_field_value_missing() -> None:
    agent = {"name": "my-agent"}

    assert _get_field_value(agent, "state") == ""
    assert _get_field_value(agent, "host.name") == ""


def test_get_field_value_deeply_nested_missing() -> None:
    """When an intermediate value is not a dict, returns empty string."""
    agent: dict[str, Any] = {"host": "not-a-dict"}

    assert _get_field_value(agent, "host.name") == ""


def test_get_field_value_none_value() -> None:
    """When the field value is explicitly None, returns empty string."""
    agent: dict[str, Any] = {"name": None}

    assert _get_field_value(agent, "name") == ""


# --- _build_table tests ---


def test_build_table_single_agent() -> None:
    """Verify that _build_table builds a row from agent data."""
    agents = [_make_agent_dict("agent-abc123", name="my-bot", state="RUNNING")]

    rows = _build_table(agents, _DEFAULT_DISPLAY_FIELDS)

    assert len(rows) == 1
    row = rows[0]
    # name, id, state, host.provider_name, host.state
    assert row[0] == "my-bot"
    assert row[1] == "agent-abc123"
    assert row[2] == "RUNNING"
    assert row[3] == "local"
    assert row[4] == "RUNNING"


def test_build_table_multiple_agents() -> None:
    """Verify that _build_table handles multiple agents."""
    agents = [
        _make_agent_dict("agent-aaa", name="bot-1", state="RUNNING"),
        _make_agent_dict("agent-bbb", name="bot-2", state="STOPPED"),
    ]

    rows = _build_table(agents, _DEFAULT_DISPLAY_FIELDS)

    assert len(rows) == 2
    assert rows[0][0] == "bot-1"
    assert rows[1][0] == "bot-2"


def test_build_table_empty() -> None:
    """Verify that _build_table returns empty list for no agents."""
    rows = _build_table([], _DEFAULT_DISPLAY_FIELDS)

    assert rows == []


def test_build_table_shows_provider() -> None:
    """Verify that _build_table includes provider info."""
    agents = [_make_agent_dict("agent-abc", name="remote-bot", provider="modal")]

    rows = _build_table(agents, _DEFAULT_DISPLAY_FIELDS)

    assert len(rows) == 1
    # host.provider_name is the 4th field in _DEFAULT_DISPLAY_FIELDS
    assert rows[0][3] == "modal"


# --- _emit_human_output tests ---


def test_emit_human_output_with_agents_includes_agent_name() -> None:
    """Verify that _emit_human_output includes the agent name in output."""
    agents = [_make_agent_dict("agent-abc123", name="my-bot", state="RUNNING")]

    with capture_loguru_messages() as messages:
        _emit_human_output(agents, _DEFAULT_DISPLAY_FIELDS)

    combined = "".join(messages)
    assert "my-bot" in combined


def test_emit_human_output_with_empty_list_shows_no_changelings() -> None:
    """Verify that _emit_human_output shows 'No changelings found' for empty list."""
    with capture_loguru_messages() as messages:
        _emit_human_output([], _DEFAULT_DISPLAY_FIELDS)

    combined = "".join(messages)
    assert "No changelings found" in combined


def test_default_display_fields_have_header_labels() -> None:
    """Verify that the header labels mapping covers all default display fields."""
    for field in _DEFAULT_DISPLAY_FIELDS:
        assert field in _HEADER_LABELS


# --- _emit_json_output tests ---


def test_emit_json_output_produces_valid_json(capsys: Any) -> None:
    """Verify that _emit_json_output writes valid JSON to stdout."""
    agents = [_make_agent_dict("agent-abc123", name="my-bot")]

    _emit_json_output(agents)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "changelings" in parsed
    assert len(parsed["changelings"]) == 1
    assert parsed["changelings"][0]["name"] == "my-bot"


def test_emit_json_output_produces_empty_list_for_no_agents(capsys: Any) -> None:
    """Verify that _emit_json_output produces an empty changelings list."""
    _emit_json_output([])

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {"changelings": []}


def test_emit_json_output_multiple_agents(capsys: Any) -> None:
    """Verify that _emit_json_output handles multiple agents."""
    agents = [
        _make_agent_dict("agent-aaa", name="bot-1"),
        _make_agent_dict("agent-bbb", name="bot-2"),
    ]

    _emit_json_output(agents)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert len(parsed["changelings"]) == 2


# --- CLI tests ---


def test_list_help() -> None:
    """Verify that changeling list --help works."""
    result = _RUNNER.invoke(cli, ["list", "--help"])

    assert result.exit_code == 0
    assert "List deployed changelings" in result.output


def test_list_json_flag_in_help() -> None:
    """Verify that --json flag appears in list help."""
    result = _RUNNER.invoke(cli, ["list", "--help"])

    assert result.exit_code == 0
    assert "--json" in result.output
