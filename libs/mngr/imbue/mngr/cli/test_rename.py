import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.create import create
from imbue.mngr.cli.rename import rename
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists


def _create_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_name: str,
) -> Host:
    """Create an agent via the provider API without starting it (no tmux session)."""
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName(agent_name),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847293"),
        ),
    )
    return host


@pytest.mark.tmux
def test_rename_stopped_agent_updates_data_json(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming a stopped agent updates data.json."""
    agent_name = f"test-rename-stopped-{uuid4().hex}"
    new_name = f"test-renamed-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Rename failed: {result.output}"
    assert "Renamed agent:" in result.output
    assert new_name in result.output

    # Verify data.json was updated
    agents = host.get_agents()
    agent_names = [str(a.name) for a in agents]
    assert new_name in agent_names
    assert agent_name not in agent_names


@pytest.mark.tmux
def test_rename_running_agent_renames_tmux_session(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming a running agent also renames the tmux session."""
    agent_name = f"test-rename-running-{uuid4().hex}"
    new_name = f"test-renamed-running-{uuid4().hex}"
    old_session_name = f"{mngr_test_prefix}{agent_name}"
    new_session_name = f"{mngr_test_prefix}{new_name}"

    with tmux_session_cleanup(old_session_name), tmux_session_cleanup(new_session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "847294",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"
        wait_for(
            lambda: tmux_session_exists(old_session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {old_session_name} to exist",
        )

        rename_result = cli_runner.invoke(
            rename,
            [agent_name, new_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert rename_result.exit_code == 0, f"Rename failed: {rename_result.output}"
        assert "Renamed agent:" in rename_result.output

        # The old session should be gone, the new one should exist.
        # Use wait_for to tolerate brief propagation delays under heavy xdist load.
        wait_for(
            lambda: tmux_session_exists(new_session_name),
            timeout=10.0,
            error_message=f"New tmux session {new_session_name} should exist after rename",
        )
        assert not tmux_session_exists(old_session_name), "Old tmux session should not exist"


def test_rename_dry_run_does_not_change_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run shows what would happen without actually renaming."""
    agent_name = f"test-rename-dry-{uuid4().hex}"
    new_name = f"test-dry-renamed-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name, "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Dry-run failed: {result.output}"
    assert "Would rename" in result.output

    # Verify agent was NOT renamed
    agents = host.get_agents()
    agent_names = [str(a.name) for a in agents]
    assert agent_name in agent_names
    assert new_name not in agent_names


def test_rename_agent_not_found_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming a non-existent agent fails."""
    result = cli_runner.invoke(
        rename,
        ["nonexistent-agent-xyz", "new-name"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0


def test_rename_to_existing_name_fails(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming to an existing agent's name fails."""
    agent_name_1 = f"test-rename-dup1-{uuid4().hex}"
    agent_name_2 = f"test-rename-dup2-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name_1)
    _create_stopped_agent(local_provider, temp_work_dir, agent_name_2)

    result = cli_runner.invoke(
        rename,
        [agent_name_1, agent_name_2],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_rename_to_same_name_is_no_op(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming to the same name is a no-op."""
    agent_name = f"test-rename-noop-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, agent_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "already named" in result.output


@pytest.mark.tmux
def test_rename_with_agent_id(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming an agent using its ID instead of name."""
    agent_name = f"test-rename-byid-{uuid4().hex}"
    new_name = f"test-renamed-byid-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    # Find the agent ID
    agents = host.get_agents()
    agent = next(a for a in agents if str(a.name) == agent_name)
    agent_id = str(agent.id)

    result = cli_runner.invoke(
        rename,
        [agent_id, new_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Rename by ID failed: {result.output}"
    assert "Renamed agent:" in result.output

    # Verify the rename happened
    agents_after = host.get_agents()
    agent_names = [str(a.name) for a in agents_after]
    assert new_name in agent_names


@pytest.mark.tmux
def test_rename_json_output(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test rename with --format json produces valid JSON output."""
    agent_name = f"test-rename-json-{uuid4().hex}"
    new_name = f"test-renamed-json-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name, "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"JSON rename failed: {result.output}"
    output = json.loads(result.output.strip())
    assert output["old_name"] == agent_name
    assert output["new_name"] == new_name
    assert "agent_id" in output


# =============================================================================
# Offline path tests (OfflineHost.rename_agent)
# =============================================================================


class _RecordingMockProvider(MockProviderInstance):
    """MockProviderInstance whose persist_agent_data actually updates ``mock_agent_data``.

    The base mock leaves ``persist_agent_data`` as the no-op default, which
    would lose the rename. This override mirrors what a real provider does:
    upsert the record keyed by ``id``.
    """

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        new_record = dict(agent_data)
        target_id = new_record.get("id")
        for i, record in enumerate(self.mock_agent_data):
            if record.get("id") == target_id:
                self.mock_agent_data[i] = new_record
                return
        self.mock_agent_data.append(new_record)


def _make_offline_host_with_agent(
    temp_mngr_ctx: MngrContext,
    agent_record: dict[str, object],
) -> OfflineHost:
    """Construct an OfflineHost backed by a recording mock provider."""
    host_id = HostId.generate()
    provider = _RecordingMockProvider(
        name=ProviderInstanceName("mock"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_agent_data=[agent_record],
    )
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="offline-host",
        stop_reason=HostState.STOPPED.value,
        created_at=now,
        updated_at=now,
    )
    return OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=provider,
        mngr_ctx=temp_mngr_ctx,
    )


def _make_offline_agent_ref(host: OfflineHost, agent_id: AgentId, agent_name: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host.id,
        agent_id=agent_id,
        agent_name=AgentName(agent_name),
        provider_name=host.provider_instance.name,
        certified_data={"id": str(agent_id), "name": agent_name},
    )


def test_offline_host_rename_agent_updates_persisted_data(
    temp_mngr_ctx: MngrContext,
) -> None:
    """OfflineHost.rename_agent should rewrite name (and optionally labels) in persisted data."""
    agent_id = AgentId.generate()
    host = _make_offline_host_with_agent(
        temp_mngr_ctx,
        {"id": str(agent_id), "name": "offline-agent", "labels": {"existing": "old"}},
    )
    agent_ref = _make_offline_agent_ref(host, agent_id, "offline-agent")

    updated_ref = host.rename_agent(
        agent_ref,
        AgentName("renamed-offline"),
        labels_to_merge={"new_key": "new_val", "existing": "updated"},
    )

    assert str(updated_ref.agent_name) == "renamed-offline"
    assert updated_ref.agent_id == agent_id

    records = host.provider_instance.list_persisted_agent_data_for_host(host.id)
    assert len(records) == 1
    assert records[0]["name"] == "renamed-offline"
    assert records[0]["labels"] == {"existing": "updated", "new_key": "new_val"}


def test_offline_host_rename_agent_no_labels_preserves_existing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """rename_agent with no labels_to_merge should leave existing labels intact."""
    agent_id = AgentId.generate()
    host = _make_offline_host_with_agent(
        temp_mngr_ctx,
        {"id": str(agent_id), "name": "before", "labels": {"keep": "me"}},
    )
    agent_ref = _make_offline_agent_ref(host, agent_id, "before")

    host.rename_agent(agent_ref, AgentName("after"), labels_to_merge=None)

    records = host.provider_instance.list_persisted_agent_data_for_host(host.id)
    assert records[0]["name"] == "after"
    assert records[0]["labels"] == {"keep": "me"}


def test_offline_host_rename_agent_raises_when_agent_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    """rename_agent should raise when the agent is missing from persisted data."""
    agent_id = AgentId.generate()
    other_id = AgentId.generate()
    host = _make_offline_host_with_agent(
        temp_mngr_ctx,
        {"id": str(other_id), "name": "someone-else"},
    )
    agent_ref = _make_offline_agent_ref(host, agent_id, "missing")

    with pytest.raises(AgentNotFoundOnHostError):
        host.rename_agent(agent_ref, AgentName("whatever"), labels_to_merge=None)
