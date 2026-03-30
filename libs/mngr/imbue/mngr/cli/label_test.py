import json
from pathlib import Path
from typing import Any
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.cli.label import _merge_labels
from imbue.mngr.cli.label import _output
from imbue.mngr.cli.label import _output_result
from imbue.mngr.cli.label import apply_labels_to_agents_offline
from imbue.mngr.cli.label import label
from imbue.mngr.cli.label import parse_label_string
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.docker.host_store import DockerHostStore
from imbue.mngr.providers.local.volume import LocalVolume


def _make_output_opts(fmt: OutputFormat = OutputFormat.HUMAN) -> OutputOptions:
    return OutputOptions(output_format=fmt, format_template=None)


# =============================================================================
# Pure function tests
# =============================================================================


@pytest.mark.parametrize(
    ("input_str", "expected_key", "expected_value"),
    [
        pytest.param("archived_at=2026-03-15", "archived_at", "2026-03-15", id="simple"),
        pytest.param("note=a=b=c", "note", "a=b=c", id="value_with_equals"),
        pytest.param("status=", "status", "", id="empty_value"),
    ],
)
def test_parse_label_string_valid(input_str: str, expected_key: str, expected_value: str) -> None:
    """parse_label_string should correctly parse valid KEY=VALUE strings."""
    key, value = parse_label_string(input_str)
    assert key == expected_key
    assert value == expected_value


@pytest.mark.parametrize(
    ("input_str", "match_text"),
    [
        pytest.param("noequalssign", "KEY=VALUE", id="no_equals"),
        pytest.param("=value", "key cannot be empty", id="empty_key"),
    ],
)
def test_parse_label_string_invalid(input_str: str, match_text: str) -> None:
    """parse_label_string should raise UserInputError on invalid input."""
    with pytest.raises(UserInputError, match=match_text):
        parse_label_string(input_str)


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        pytest.param({"a": "1"}, {"b": "2"}, {"a": "1", "b": "2"}, id="adds_new"),
        pytest.param({"a": "1", "b": "2"}, {"a": "updated"}, {"a": "updated", "b": "2"}, id="overwrites"),
        pytest.param({}, {"key": "value"}, {"key": "value"}, id="empty_current"),
    ],
)
def test_merge_labels(current: dict[str, str], new: dict[str, str], expected: dict[str, str]) -> None:
    """_merge_labels should correctly merge labels."""
    assert _merge_labels(current, new) == expected


# =============================================================================
# Output tests
# =============================================================================


def test_output_human(capsys) -> None:
    """_output should write message to stdout in HUMAN format."""
    _output("test message", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "test message" in captured.out


def test_output_json_silent(capsys) -> None:
    """_output should be silent in JSON format."""
    _output("test message", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_output_result_human(capsys) -> None:
    """_output_result in HUMAN format shows change count."""
    changes: list[dict[str, Any]] = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "1 agent(s)" in captured.out


def test_output_result_json(capsys) -> None:
    """_output_result in JSON format emits structured JSON."""
    changes: list[dict[str, Any]] = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["count"] == 1
    assert len(output["changes"]) == 1


def test_output_result_jsonl(capsys) -> None:
    """_output_result in JSONL format emits event with data."""
    changes: list[dict[str, Any]] = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.JSONL))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "label_result"
    assert output["count"] == 1


def test_output_result_empty_changes(capsys) -> None:
    """_output_result in HUMAN format should be silent when no changes."""
    _output_result([], _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert captured.out == ""


# =============================================================================
# CLI validation tests
# =============================================================================


def test_label_requires_label_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """label command should fail when no --label is provided."""
    result = cli_runner.invoke(
        label,
        ["my-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_label_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """label command should fail when no agent is specified."""
    result = cli_runner.invoke(
        label,
        ["--label", "key=value"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


# =============================================================================
# Integration tests (online path)
# =============================================================================


def test_label_applies_labels_to_agent(
    local_host: Host,
    temp_work_dir: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Label command should apply labels to an agent on a local host."""
    agent = create_test_agent_state(local_host, temp_work_dir, "label-test-agent")
    assert agent.get_labels() == {}

    result = cli_runner.invoke(
        label,
        ["label-test-agent", "--label", "env=prod", "--label", "team=backend"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert agent.get_labels() == {"env": "prod", "team": "backend"}


def test_label_merges_with_existing_labels(
    local_host: Host,
    temp_work_dir: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Label command should merge new labels with existing ones."""
    agent = create_test_agent_state(local_host, temp_work_dir, "merge-label-agent")
    agent.set_labels({"existing": "value", "overwrite_me": "old"})

    result = cli_runner.invoke(
        label,
        ["merge-label-agent", "--label", "overwrite_me=new", "--label", "added=yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert agent.get_labels() == {"existing": "value", "overwrite_me": "new", "added": "yes"}


def test_label_json_output(
    local_host: Host,
    temp_work_dir: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Label command should produce valid JSON output with --format json."""
    create_test_agent_state(local_host, temp_work_dir, "json-label-agent")

    result = cli_runner.invoke(
        label,
        ["json-label-agent", "--label", "key=value", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    output = json.loads(result.output.strip())
    assert output["count"] == 1
    assert output["changes"][0]["labels"]["key"] == "value"


# =============================================================================
# Offline path tests
# =============================================================================


def test_apply_labels_offline_updates_persisted_data(
    tmp_path: Path,
) -> None:
    """apply_labels_to_agents_offline should merge labels in persisted data."""
    # Set up a DockerHostStore backed by a local filesystem volume
    vol_path = tmp_path / "state_vol"
    vol_path.mkdir()
    volume = LocalVolume(root_path=vol_path)
    store = DockerHostStore(volume=volume)

    host_id = HostId.generate()
    agent_id = AgentId.generate()

    # Seed persisted agent data with existing labels
    store.persist_agent_data(
        host_id,
        {"id": str(agent_id), "name": "offline-agent", "labels": {"existing": "old"}},
    )

    agent_match = AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("offline-agent"),
        host_id=host_id,
        host_name=HostName("offline-host"),
        provider_name=ProviderInstanceName("docker"),
    )

    changes: list[dict[str, Any]] = []
    apply_labels_to_agents_offline(
        provider=cast(BaseProviderInstance, store),
        host_id=host_id,
        agent_matches=[agent_match],
        labels_to_set={"new_key": "new_val", "existing": "updated"},
        output_opts=_make_output_opts(),
        changes=changes,
    )

    assert len(changes) == 1
    assert changes[0]["labels"] == {"existing": "updated", "new_key": "new_val"}

    # Verify the persisted data was actually written
    records = store.list_persisted_agent_data_for_host(host_id)
    assert len(records) == 1
    assert records[0]["labels"] == {"existing": "updated", "new_key": "new_val"}


def test_apply_labels_offline_raises_when_agent_not_found(
    tmp_path: Path,
) -> None:
    """apply_labels_to_agents_offline should raise when agent is missing from persisted data."""
    vol_path = tmp_path / "state_vol"
    vol_path.mkdir()
    volume = LocalVolume(root_path=vol_path)
    store = DockerHostStore(volume=volume)

    host_id = HostId.generate()
    agent_id = AgentId.generate()

    # No persisted data seeded -- agent does not exist in store
    agent_match = AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("missing-agent"),
        host_id=host_id,
        host_name=HostName("offline-host"),
        provider_name=ProviderInstanceName("docker"),
    )

    changes: list[dict[str, Any]] = []
    with pytest.raises(AgentNotFoundOnHostError):
        apply_labels_to_agents_offline(
            provider=cast(BaseProviderInstance, store),
            host_id=host_id,
            agent_matches=[agent_match],
            labels_to_set={"key": "val"},
            output_opts=_make_output_opts(),
            changes=changes,
        )
