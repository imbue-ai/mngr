import json
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api import providers as providers_module
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import CertifiedHostData
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_autocompact import cli as cli_module
from imbue.mngr_autocompact.cli import autocompact_group
from imbue.mngr_autocompact.cli import output_check_result
from imbue.mngr_autocompact.cli import output_run_result


def test_autocompact_group_shows_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(autocompact_group, ["--help"])
    assert result.exit_code == 0
    assert "--help" in result.output
    assert "check" in result.output
    assert "run" in result.output


def test_autocompact_check_shows_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(autocompact_group, ["check", "--help"])
    assert result.exit_code == 0
    assert "--all" in result.output
    assert "--dry-run" not in result.output


def test_autocompact_run_shows_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(autocompact_group, ["run", "--help"])
    assert result.exit_code == 0
    assert "--all" in result.output
    assert "--dry-run" not in result.output


def test_autocompact_check_requires_target_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["check"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "Specify an agent target or use --all" in result.output


def test_autocompact_check_rejects_both_target_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["check", "my-agent", "--all"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "Cannot specify both an agent target and --all" in result.output


def test_autocompact_check_rejects_dry_run_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["check", "--all", "--dry-run"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "no such option" in result.output.lower() or "unrecognized" in result.output.lower()


def test_autocompact_run_requires_target_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["run"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "Specify an agent target or use --all" in result.output


def test_autocompact_run_rejects_both_target_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["run", "my-agent", "--all"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "Cannot specify both an agent target and --all" in result.output


def test_output_check_result_human_no_agents(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    output_check_result([], output_opts=output_opts)
    out = capsys.readouterr().out
    assert "No agents require compaction." in out


def test_output_check_result_human_would_compact(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    output_check_result([AgentName("agent-1"), AgentName("agent-2")], output_opts=output_opts)
    out = capsys.readouterr().out
    assert "Would compact 2 agent(s): agent-1, agent-2" in out


def test_output_check_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    output_check_result([AgentName("agent-1")], output_opts=output_opts)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["agents"] == ["agent-1"]


def test_output_check_result_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(is_quiet=True)
    output_check_result([AgentName("agent-1")], output_opts=output_opts)
    out = capsys.readouterr().out
    assert out == ""


def test_output_run_result_human_no_compacted(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    output_run_result([], output_opts=output_opts)
    out = capsys.readouterr().out
    assert "No agents require compaction." in out


def test_output_run_result_human_compacted(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    output_run_result([AgentName("agent-1"), AgentName("agent-2")], output_opts=output_opts)
    out = capsys.readouterr().out
    assert "Compacted 2 agent(s): agent-1, agent-2" in out


def test_output_run_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    output_run_result([AgentName("agent-1")], output_opts=output_opts)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["compacted"] == ["agent-1"]


def test_output_run_result_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(is_quiet=True)
    output_run_result([AgentName("agent-1")], output_opts=output_opts)
    out = capsys.readouterr().out
    assert out == ""


def test_autocompact_check_all_executes(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["check", "--all"], obj=plugin_manager)
    assert result.exit_code == 0
    assert "No agents require compaction." in result.output


def test_autocompact_run_all_executes(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["run", "--all"], obj=plugin_manager)
    assert result.exit_code == 0
    assert "No agents require compaction." in result.output


def test_autocompact_check_target_not_found(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["check", "non-existent"], obj=plugin_manager)
    assert result.exit_code != 0


def test_autocompact_run_target_not_found(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(autocompact_group, ["run", "non-existent"], obj=plugin_manager)
    assert result.exit_code != 0


class _FakeOnlineHost(Host):
    test_agents: list[Any] = Field(default_factory=list)

    def get_agents(self) -> list[Any]:
        return self.test_agents


class _DummyNonCompactionAgent:
    def __init__(self, id: AgentId, name: AgentName, agent_type: AgentTypeName, running: bool = True) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.running = running

    def is_running(self) -> bool:
        return self.running


class _DummyCompactionAgent(HasCompactionMixin):
    def __init__(
        self,
        id: AgentId,
        name: AgentName,
        agent_type: AgentTypeName,
        running: bool = True,
        mngr_ctx: MngrContext | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.running = running
        self.mngr_ctx = mngr_ctx
        self.compaction_count = 0

    def is_running(self) -> bool:
        return self.running

    def request_compaction(self, instructions: str | None = None) -> None:
        self.compaction_count += 1

    def get_cache_ttl_minutes(self) -> int | None:
        return 60

    def get_context_tokens(self) -> int | None:
        return 100_000

    def get_idle_since(self) -> datetime | None:
        return None


class _FakeDiscoveryProvider(LocalProviderInstance):
    mock_agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = Field(default_factory=dict)
    mock_hosts: dict[HostId, HostInterface] = Field(default_factory=dict)

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        return self.mock_agents_by_host

    def get_host(self, host: HostId | HostName) -> Host:
        if isinstance(host, HostId) and host in self.mock_hosts:
            return cast(Host, self.mock_hosts[host])
        return super().get_host(host)


def test_resolve_target_agent_host_offline(temp_mngr_ctx: MngrContext) -> None:
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(
        config=config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )
    hid = HostId.generate()
    off_host = OfflineHost(
        id=hid,
        certified_host_data=CertifiedHostData(
            host_id=str(hid),
            host_name="off-host",
            created_at=now,
            updated_at=now,
        ),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
    )
    provider.mock_hosts[hid] = off_host
    agent_id = AgentId.generate()
    host_ref = DiscoveredHost(
        host_id=hid,
        host_name=HostName("off-host"),
        provider_name=provider.name,
        host_state=HostState.STOPPED,
    )
    agent_ref = DiscoveredAgent(
        agent_id=agent_id,
        agent_name=AgentName("off-agent"),
        host_id=hid,
        provider_name=provider.name,
    )
    provider.mock_agents_by_host[host_ref] = [agent_ref]
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider

    try:
        with pytest.raises(UserInputError, match="Host 'off-host' is offline"):
            cli_module._resolve_target_agent(AgentAddress(agent=AgentName("off-agent")), mngr_ctx)
    finally:
        reset_provider_instances()


def test_resolve_target_agent_not_running_or_missing(temp_mngr_ctx: MngrContext) -> None:
    config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(
        config=config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )
    hid = HostId.generate()
    stopped_agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("stopped-agent"),
        agent_type=AgentTypeName("claude"),
        running=False,
    )
    on_host = _FakeOnlineHost(
        id=hid,
        host_name=HostName("on-host"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
        test_agents=[stopped_agent],
    )
    provider.mock_hosts[hid] = on_host

    host_ref = DiscoveredHost(
        host_id=hid,
        host_name=HostName("on-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    missing_id = AgentId.generate()
    missing_ref = DiscoveredAgent(
        agent_id=missing_id,
        agent_name=AgentName("missing-agent"),
        host_id=hid,
        provider_name=provider.name,
    )
    stopped_ref = DiscoveredAgent(
        agent_id=stopped_agent.id,
        agent_name=stopped_agent.name,
        host_id=hid,
        provider_name=provider.name,
    )
    provider.mock_agents_by_host[host_ref] = [missing_ref, stopped_ref]
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider

    try:
        with pytest.raises(UserInputError, match="Agent 'missing-agent' is not running on host 'on-host'"):
            cli_module._resolve_target_agent(AgentAddress(agent=AgentName("missing-agent")), mngr_ctx)

        with pytest.raises(UserInputError, match="Agent 'stopped-agent' is not running on host 'on-host'"):
            cli_module._resolve_target_agent(AgentAddress(agent=AgentName("stopped-agent")), mngr_ctx)
    finally:
        reset_provider_instances()


def test_resolve_target_agent_non_compaction(temp_mngr_ctx: MngrContext) -> None:
    config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(
        config=config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )
    hid = HostId.generate()
    raw_agent = _DummyNonCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("raw-agent"),
        agent_type=AgentTypeName("raw"),
        running=True,
    )
    on_host = _FakeOnlineHost(
        id=hid,
        host_name=HostName("on-host"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
        test_agents=[raw_agent],
    )
    provider.mock_hosts[hid] = on_host

    host_ref = DiscoveredHost(
        host_id=hid,
        host_name=HostName("on-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    raw_ref = DiscoveredAgent(
        agent_id=raw_agent.id,
        agent_name=raw_agent.name,
        host_id=hid,
        provider_name=provider.name,
    )
    provider.mock_agents_by_host[host_ref] = [raw_ref]
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider

    try:
        with pytest.raises(UserInputError, match="does not support context compaction"):
            cli_module._resolve_target_agent(AgentAddress(agent=AgentName("raw-agent")), mngr_ctx)
    finally:
        reset_provider_instances()


def test_resolve_target_agent_success(temp_mngr_ctx: MngrContext) -> None:
    config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(
        config=config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )
    hid = HostId.generate()
    comp_agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("comp-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
    )
    on_host = _FakeOnlineHost(
        id=hid,
        host_name=HostName("on-host"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
        test_agents=[comp_agent],
    )
    provider.mock_hosts[hid] = on_host

    host_ref = DiscoveredHost(
        host_id=hid,
        host_name=HostName("on-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    comp_ref = DiscoveredAgent(
        agent_id=comp_agent.id,
        agent_name=comp_agent.name,
        host_id=hid,
        provider_name=provider.name,
    )
    provider.mock_agents_by_host[host_ref] = [comp_ref]
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider

    try:
        resolved = cli_module._resolve_target_agent(AgentAddress(agent=AgentName("comp-agent")), mngr_ctx)
        assert resolved.name == AgentName("comp-agent")
    finally:
        reset_provider_instances()
