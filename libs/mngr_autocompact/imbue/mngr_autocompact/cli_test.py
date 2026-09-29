import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api import providers as providers_module
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import AgentNotFoundError
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
from imbue.mngr.primitives import HostAddress
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


@pytest.mark.parametrize("subcommand", ["check", "run"])
@pytest.mark.parametrize("targets", [["my-agent"], ["agent-1", "agent-2"]])
def test_autocompact_rejects_both_targets_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    subcommand: str,
    targets: list[str],
) -> None:
    result = cli_runner.invoke(autocompact_group, [subcommand, *targets, "--all"], obj=plugin_manager)
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


@pytest.mark.parametrize("subcommand", ["check", "run"])
@pytest.mark.parametrize(
    ("targets", "expected_message"),
    [
        (["non-existent"], "No agent(s) found matching: non-existent"),
        (["non-existent-1", "non-existent-2"], "No agent(s) found matching: non-existent-1, non-existent-2"),
    ],
)
def test_autocompact_targets_not_found_reports_all_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    subcommand: str,
    targets: list[str],
    expected_message: str,
) -> None:
    result = cli_runner.invoke(autocompact_group, [subcommand, *targets], obj=plugin_manager)
    assert result.exit_code != 0
    assert expected_message in result.output
    assert "sub-exception" not in result.output


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


@contextmanager
def _fake_discovery_context(temp_mngr_ctx: MngrContext) -> Iterator[tuple[MngrContext, _FakeDiscoveryProvider]]:
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
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider
    try:
        yield mngr_ctx, provider
    finally:
        reset_provider_instances()


def _add_fake_online_host(
    provider: _FakeDiscoveryProvider,
    mngr_ctx: MngrContext,
    host_name: str,
    live_agents: list[Any],
    discovered_agent_names_by_id: dict[AgentId, AgentName],
) -> None:
    host_id = HostId.generate()
    provider.mock_hosts[host_id] = _FakeOnlineHost(
        id=host_id,
        host_name=HostName(host_name),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
        test_agents=live_agents,
    )
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(host_name),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_agents_by_host[host_ref] = [
        DiscoveredAgent(agent_id=agent_id, agent_name=agent_name, host_id=host_id, provider_name=provider.name)
        for agent_id, agent_name in discovered_agent_names_by_id.items()
    ]


def _make_compaction_agent(name: str, running: bool = True) -> _DummyCompactionAgent:
    return _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName(name),
        agent_type=AgentTypeName("claude"),
        running=running,
    )


def test_resolve_target_agents_host_offline(temp_mngr_ctx: MngrContext) -> None:
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        hid = HostId.generate()
        provider.mock_hosts[hid] = OfflineHost(
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
        host_ref = DiscoveredHost(
            host_id=hid,
            host_name=HostName("off-host"),
            provider_name=provider.name,
            host_state=HostState.STOPPED,
        )
        agent_ref = DiscoveredAgent(
            agent_id=AgentId.generate(),
            agent_name=AgentName("off-agent"),
            host_id=hid,
            provider_name=provider.name,
        )
        provider.mock_agents_by_host[host_ref] = [agent_ref]

        with pytest.raises(UserInputError, match="Host 'off-host' is offline"):
            cli_module._resolve_target_agents([AgentAddress(agent=AgentName("off-agent"))], mngr_ctx)


def test_resolve_target_agents_not_running_or_missing(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        stopped_agent = _make_compaction_agent("stopped-agent", running=False)
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[stopped_agent],
            discovered_agent_names_by_id={
                AgentId.generate(): AgentName("missing-agent"),
                stopped_agent.id: stopped_agent.name,
            },
        )

        with pytest.raises(UserInputError, match="Agent 'missing-agent' is not running on host 'on-host'"):
            cli_module._resolve_target_agents([AgentAddress(agent=AgentName("missing-agent"))], mngr_ctx)

        with pytest.raises(UserInputError, match="Agent 'stopped-agent' is not running on host 'on-host'"):
            cli_module._resolve_target_agents([AgentAddress(agent=AgentName("stopped-agent"))], mngr_ctx)


def test_resolve_target_agents_non_compaction(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        raw_agent = _DummyNonCompactionAgent(
            id=AgentId.generate(),
            name=AgentName("raw-agent"),
            agent_type=AgentTypeName("raw"),
            running=True,
        )
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[raw_agent],
            discovered_agent_names_by_id={raw_agent.id: raw_agent.name},
        )

        warnings: list[str] = []
        sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
        try:
            resolved = cli_module._resolve_target_agents([AgentAddress(agent=AgentName("raw-agent"))], mngr_ctx)
        finally:
            logger.remove(sink_id)

        assert resolved == []
        assert any("raw-agent" in w and "does not support context compaction" in w for w in warnings)


def test_resolve_target_agents_mixed_compaction_support(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        raw_agent = _DummyNonCompactionAgent(
            id=AgentId.generate(),
            name=AgentName("raw-agent"),
            agent_type=AgentTypeName("raw"),
            running=True,
        )
        comp_agent = _make_compaction_agent("comp-agent")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[raw_agent, comp_agent],
            discovered_agent_names_by_id={raw_agent.id: raw_agent.name, comp_agent.id: comp_agent.name},
        )

        warnings: list[str] = []
        sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
        try:
            resolved = cli_module._resolve_target_agents(
                [AgentAddress(agent=AgentName("raw-agent")), AgentAddress(agent=AgentName("comp-agent"))],
                mngr_ctx,
            )
        finally:
            logger.remove(sink_id)

        assert resolved == [comp_agent]
        assert any("raw-agent" in w and "does not support context compaction" in w for w in warnings)


def test_resolve_target_agents_single(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        comp_agent = _make_compaction_agent("comp-agent")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[comp_agent],
            discovered_agent_names_by_id={comp_agent.id: comp_agent.name},
        )

        resolved = cli_module._resolve_target_agents([AgentAddress(agent=AgentName("comp-agent"))], mngr_ctx)
        assert resolved == [comp_agent]


def test_resolve_target_agents_across_hosts_returns_only_targeted_agents(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        agent_a1 = _make_compaction_agent("agent-a1")
        agent_a2 = _make_compaction_agent("agent-a2")
        agent_b1 = _make_compaction_agent("agent-b1")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-a",
            live_agents=[agent_a1, agent_a2],
            discovered_agent_names_by_id={agent_a1.id: agent_a1.name, agent_a2.id: agent_a2.name},
        )
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-b",
            live_agents=[agent_b1],
            discovered_agent_names_by_id={agent_b1.id: agent_b1.name},
        )

        targets = [AgentAddress(agent=AgentName("agent-a2")), AgentAddress(agent=AgentName("agent-b1"))]
        resolved = cli_module._resolve_target_agents(targets, mngr_ctx)
        assert sorted(agent.name for agent in resolved) == [AgentName("agent-a2"), AgentName("agent-b1")]


def test_resolve_target_agents_deduplicates_name_and_id_of_same_agent(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        comp_agent = _make_compaction_agent("comp-agent")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[comp_agent],
            discovered_agent_names_by_id={comp_agent.id: comp_agent.name},
        )

        targets = [
            AgentAddress(agent=AgentName("comp-agent")),
            AgentAddress(agent=comp_agent.id),
            AgentAddress(agent=AgentName("comp-agent")),
        ]
        resolved = cli_module._resolve_target_agents(targets, mngr_ctx)
        assert resolved == [comp_agent]


def test_resolve_target_agents_resolves_every_agent_sharing_a_name(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        agent_on_host_a = _make_compaction_agent("shared-name")
        agent_on_host_b = _make_compaction_agent("shared-name")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-a",
            live_agents=[agent_on_host_a],
            discovered_agent_names_by_id={agent_on_host_a.id: agent_on_host_a.name},
        )
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-b",
            live_agents=[agent_on_host_b],
            discovered_agent_names_by_id={agent_on_host_b.id: agent_on_host_b.name},
        )

        resolved = cli_module._resolve_target_agents([AgentAddress(agent=AgentName("shared-name"))], mngr_ctx)
        assert {id(agent) for agent in resolved} == {id(agent_on_host_a), id(agent_on_host_b)}

        resolved = cli_module._resolve_target_agents(
            [AgentAddress(agent=AgentName("shared-name"), host=HostAddress(host=HostName("host-b")))], mngr_ctx
        )
        assert resolved == [agent_on_host_b]


def test_resolve_target_agents_reports_every_unknown_target(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        comp_agent = _make_compaction_agent("comp-agent")
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "on-host",
            live_agents=[comp_agent],
            discovered_agent_names_by_id={comp_agent.id: comp_agent.name},
        )

        targets = [
            AgentAddress(agent=AgentName("unknown-agent-1")),
            AgentAddress(agent=AgentName("comp-agent")),
            AgentAddress(agent=AgentName("unknown-agent-2")),
        ]
        with pytest.raises(AgentNotFoundError) as exc_info:
            cli_module._resolve_target_agents(targets, mngr_ctx)
        assert "unknown-agent-1" in str(exc_info.value)
        assert "unknown-agent-2" in str(exc_info.value)


def test_resolve_target_agents_raises_user_error_unwrapped_from_worker(temp_mngr_ctx: MngrContext) -> None:
    with _fake_discovery_context(temp_mngr_ctx) as (mngr_ctx, provider):
        running_agent = _make_compaction_agent("running-agent")
        stopped_agent = _make_compaction_agent("stopped-agent", running=False)
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-a",
            live_agents=[running_agent],
            discovered_agent_names_by_id={running_agent.id: running_agent.name},
        )
        _add_fake_online_host(
            provider,
            mngr_ctx,
            "host-b",
            live_agents=[stopped_agent],
            discovered_agent_names_by_id={stopped_agent.id: stopped_agent.name},
        )

        targets = [AgentAddress(agent=AgentName("running-agent")), AgentAddress(agent=AgentName("stopped-agent"))]
        with pytest.raises(UserInputError, match="Agent 'stopped-agent' is not running on host 'host-b'"):
            cli_module._resolve_target_agents(targets, mngr_ctx)
