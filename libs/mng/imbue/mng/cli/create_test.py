"""Tests for create module helper functions."""

from pathlib import Path
from typing import Any
from typing import cast

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mng.cli.agent_addr import AgentAddress
from imbue.mng.cli.agent_addr import parse_agent_address
from imbue.mng.cli.create import _CreateCommand
from imbue.mng.cli.create import _is_creating_new_host
from imbue.mng.cli.create import _parse_agent_opts
from imbue.mng.cli.create import _parse_branch_flag
from imbue.mng.cli.create import _parse_host_lifecycle_options
from imbue.mng.cli.create import _parse_project_name
from imbue.mng.cli.create import _resolve_source_location
from imbue.mng.cli.create import _resolve_target_host
from imbue.mng.cli.create import _split_cli_args
from imbue.mng.cli.create import _try_reuse_existing_agent
from imbue.mng.cli.create import create
from imbue.mng.config.data_types import CreateCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance

# =============================================================================
# Tests for _CreateCommand.parse_args (-- passthrough arg handling)
# =============================================================================

# Minimal command using _CreateCommand with the same argument declarations as
# the real create command, but that simply records the parsed params.
# Note: the real create command receives all params via **kwargs so does not
# need to worry about shadowing the 'type' builtin; here we use ctx.params
# directly and avoid accepting 'type' as a Python parameter name.
_captured_params: dict[str, Any] = {}


@click.command(cls=_CreateCommand)
@click.argument("positional_name", default=None, required=False)
@click.argument("positional_agent_type", default=None, required=False)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
@click.option("--type")
@click.option("--name")
@click.pass_context
def _test_create_cmd(ctx: click.Context, **kwargs: Any) -> None:
    _captured_params.clear()
    _captured_params.update(ctx.params)


def _run_test_create(args: list[str]) -> dict[str, Any]:
    """Invoke the test command and return the parsed params."""
    runner = CliRunner()
    result = runner.invoke(_test_create_cmd, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return dict(_captured_params)


def test_create_command_type_flag_with_dash_dash_passthrough() -> None:
    """Regression: --type with -- passthrough must not leak into positional_agent_type."""
    params = _run_test_create(["selene", "--type", "claude", "--", "--dangerously-skip-permissions"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] is None
    assert params["agent_args"] == ("--dangerously-skip-permissions",)
    assert params["type"] == "claude"


def test_create_command_positional_name_and_type_with_dash_dash() -> None:
    """Positional name + type before -- should work, after-dash args go to agent_args."""
    params = _run_test_create(["selene", "claude", "--", "--flag", "extra"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] == "claude"
    assert params["agent_args"] == ("--flag", "extra")


def test_create_command_type_flag_with_multiple_dash_dash_args() -> None:
    """Multiple args after -- must all go to agent_args."""
    params = _run_test_create(["selene", "--type", "claude", "--", "arg1", "arg2"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] is None
    assert params["agent_args"] == ("arg1", "arg2")
    assert params["type"] == "claude"


def test_create_command_no_dash_dash() -> None:
    """Without --, positional args fill name and type normally."""
    params = _run_test_create(["selene", "claude"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] == "claude"
    assert params["agent_args"] == ()


def test_create_command_bare_dash_dash() -> None:
    """Bare -- with nothing after it produces empty agent_args."""
    params = _run_test_create(["selene", "--type", "claude", "--"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] is None
    assert params["agent_args"] == ()
    assert params["type"] == "claude"


def test_create_command_no_positional_name_with_type_and_dash_dash() -> None:
    """No positional name + --type + -- must not leak after-dash into positional_name."""
    params = _run_test_create(["--type", "claude", "--", "--dangerously-skip-permissions"])

    assert params["positional_name"] is None
    assert params["positional_agent_type"] is None
    assert params["agent_args"] == ("--dangerously-skip-permissions",)
    assert params["type"] == "claude"


def test_create_command_pre_and_post_dash_agent_args_merged() -> None:
    """Extra positional args before -- merge with args after --."""
    params = _run_test_create(["selene", "claude", "extra", "--", "--flag"])

    assert params["positional_name"] == "selene"
    assert params["positional_agent_type"] == "claude"
    assert params["agent_args"] == ("extra", "--flag")


# =============================================================================
# Tests for _parse_host_lifecycle_options
# =============================================================================


def test_parse_host_lifecycle_options_all_none(default_create_cli_opts: CreateCliOptions) -> None:
    """When all CLI options are None, result should have all None values."""
    result = _parse_host_lifecycle_options(default_create_cli_opts)

    assert result.idle_timeout_seconds is None
    assert result.idle_mode is None
    assert result.activity_sources is None


def test_parse_host_lifecycle_options_with_idle_timeout(default_create_cli_opts: CreateCliOptions) -> None:
    """idle_timeout should be parsed as a duration string."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().idle_timeout, "10m"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.idle_timeout_seconds == 600
    assert result.idle_mode is None
    assert result.activity_sources is None


def test_parse_host_lifecycle_options_with_idle_mode_lowercase(default_create_cli_opts: CreateCliOptions) -> None:
    """idle_mode should be parsed and uppercased to IdleMode enum."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().idle_mode, "agent"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.idle_timeout_seconds is None
    assert result.idle_mode == IdleMode.AGENT
    assert result.activity_sources is None


def test_parse_host_lifecycle_options_with_idle_mode_uppercase(default_create_cli_opts: CreateCliOptions) -> None:
    """idle_mode should work with uppercase input."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().idle_mode, "SSH"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.idle_mode == IdleMode.SSH


def test_parse_host_lifecycle_options_with_activity_sources_single(default_create_cli_opts: CreateCliOptions) -> None:
    """activity_sources should parse a single source."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().activity_sources, "boot"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.activity_sources == (ActivitySource.BOOT,)


def test_parse_host_lifecycle_options_with_activity_sources_multiple(
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """activity_sources should parse comma-separated sources."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().activity_sources, "boot,ssh,agent"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.activity_sources == (ActivitySource.BOOT, ActivitySource.SSH, ActivitySource.AGENT)


def test_parse_host_lifecycle_options_with_activity_sources_whitespace(
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """activity_sources should handle whitespace around commas."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().activity_sources, "boot , ssh , agent"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.activity_sources == (ActivitySource.BOOT, ActivitySource.SSH, ActivitySource.AGENT)


def test_parse_host_lifecycle_options_all_provided(default_create_cli_opts: CreateCliOptions) -> None:
    """All options should be correctly parsed when all are provided."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().idle_timeout, "30m"),
        to_update(default_create_cli_opts.field_ref().idle_mode, "disabled"),
        to_update(default_create_cli_opts.field_ref().activity_sources, "create,process"),
    )

    result = _parse_host_lifecycle_options(opts)

    assert result.idle_timeout_seconds == 1800
    assert result.idle_mode == IdleMode.DISABLED
    assert result.activity_sources == (ActivitySource.CREATE, ActivitySource.PROCESS)


# =============================================================================
# Tests for _try_reuse_existing_agent
# =============================================================================

# Valid 32-character hex strings for test IDs
TEST_HOST_ID_1 = "host-00000000000000000000000000000001"
TEST_HOST_ID_2 = "host-00000000000000000000000000000002"
TEST_AGENT_ID_1 = "agent-00000000000000000000000000000001"
TEST_AGENT_ID_2 = "agent-00000000000000000000000000000002"


def _make_discovered_host(
    provider: str = "local", host_id: str = TEST_HOST_ID_1, host_name: str = "test-host"
) -> DiscoveredHost:
    return DiscoveredHost(
        provider_name=ProviderInstanceName(provider),
        host_id=HostId(host_id),
        host_name=HostName(host_name),
    )


def _make_discovered_agent(
    agent_id: str = TEST_AGENT_ID_1,
    agent_name: str = "test-agent",
    host_id: str = TEST_HOST_ID_1,
    provider: str = "local",
) -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=AgentId(agent_id),
        agent_name=AgentName(agent_name),
        host_id=HostId(host_id),
        provider_name=ProviderInstanceName(provider),
    )


# -- Filtering tests (function returns early, no provider/host interaction) --


def test_try_reuse_existing_agent_no_agents_found(temp_mng_ctx: MngContext) -> None:
    """Returns None when no agents match the name."""
    result = _try_reuse_existing_agent(
        agent_name=AgentName("nonexistent"),
        provider_name=None,
        target_host_ref=None,
        mng_ctx=temp_mng_ctx,
        agent_and_host_loader=lambda: {},
    )

    assert result is None


def test_try_reuse_existing_agent_no_matching_name(temp_mng_ctx: MngContext) -> None:
    """Returns None when agents exist but none match the name."""
    host_ref = _make_discovered_host()
    agent_ref = _make_discovered_agent(agent_name="other-agent")

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=None,
        target_host_ref=None,
        mng_ctx=temp_mng_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


def test_try_reuse_existing_agent_filters_by_provider(temp_mng_ctx: MngContext) -> None:
    """Returns None when agent exists but on different provider."""
    host_ref = _make_discovered_host(provider="modal")
    agent_ref = _make_discovered_agent(agent_name="test-agent", provider="modal")

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
        target_host_ref=None,
        mng_ctx=temp_mng_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


def test_try_reuse_existing_agent_filters_by_host(temp_mng_ctx: MngContext) -> None:
    """Returns None when agent exists but on different host."""
    host_ref = _make_discovered_host(host_id=TEST_HOST_ID_1)
    agent_ref = _make_discovered_agent(agent_name="test-agent", host_id=TEST_HOST_ID_1)

    target_host_ref = _make_discovered_host(host_id=TEST_HOST_ID_2)

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=None,
        target_host_ref=target_host_ref,
        mng_ctx=temp_mng_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


# -- Tests using real local provider infrastructure --


@pytest.mark.tmux
def test_try_reuse_existing_agent_found_and_started(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Returns (agent, host) when agent is found and started."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))

    # Create a real agent on the local host with a harmless command
    agent_options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        name=AgentName("reuse-test-agent"),
        command=CommandString("sleep 47291"),
    )
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=agent_options,
    )

    # Build references that match the real host and agent
    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = DiscoveredAgent(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )

    try:
        result = _try_reuse_existing_agent(
            agent_name=agent.name,
            provider_name=None,
            target_host_ref=None,
            mng_ctx=temp_mng_ctx,
            agent_and_host_loader=lambda: {host_ref: [agent_ref]},
        )

        assert result is not None
        found_agent, found_host = result
        assert found_agent.id == agent.id
        assert found_agent.name == agent.name
        assert found_host.id == local_host.id
    finally:
        local_host.stop_agents([agent.id])


def test_try_reuse_existing_agent_not_found_on_host(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    """Returns None when agent reference exists but agent not found on online host."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))

    # Build references pointing to this host, but with a nonexistent agent ID
    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = DiscoveredAgent(
        agent_id=AgentId(TEST_AGENT_ID_1),
        agent_name=AgentName("ghost-agent"),
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )

    result = _try_reuse_existing_agent(
        agent_name=AgentName("ghost-agent"),
        provider_name=None,
        target_host_ref=None,
        mng_ctx=temp_mng_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


# =============================================================================
# Tests for _resolve_source_location and _resolve_target_host with is_start_desired
# =============================================================================


def test_resolve_source_location_with_auto_start_enabled(
    default_create_cli_opts: CreateCliOptions,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """_resolve_source_location returns an online host when is_start_desired=True."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_path, str(temp_work_dir)),
    )

    result, source_agent_id = _resolve_source_location(
        opts,
        agent_and_host_loader=lambda: {},
        mng_ctx=temp_mng_ctx,
        is_start_desired=True,
    )

    assert isinstance(result.host, OnlineHostInterface)
    assert result.path == temp_work_dir
    assert source_agent_id is None


def test_resolve_target_host_with_auto_start_enabled(
    temp_mng_ctx: MngContext,
) -> None:
    """_resolve_target_host returns an online host when target is None and is_start_desired=True."""
    result = _resolve_target_host(
        target_host=None,
        mng_ctx=temp_mng_ctx,
        is_start_desired=True,
    )

    assert isinstance(result, OnlineHostInterface)


def test_resolve_target_host_with_host_reference(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    """_resolve_target_host resolves a DiscoveredHost to an online host."""
    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_provider.host_id,
        host_name=HostName("localhost"),
    )

    result = _resolve_target_host(
        target_host=host_ref,
        mng_ctx=temp_mng_ctx,
        is_start_desired=True,
    )

    assert isinstance(result, OnlineHostInterface)


# =============================================================================
# Tests for _parse_project_name project mismatch validation
# =============================================================================


def test_parse_project_name_returns_explicit_project(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """When --project is specified, return it directly without validation."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    address = AgentAddress(provider_name=ProviderInstanceName("docker"))
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().project, "explicit-project"),
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
    )

    result = _parse_project_name(source_location, opts, address, temp_mng_ctx)

    assert result == "explicit-project"


def test_parse_project_name_raises_on_mismatch_with_new_host_and_source_agent(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """Raises UserInputError when source agent project differs from local and creating a new host."""
    # Create a source directory with a different name than the CWD project
    different_project_dir = tmp_path / "totally-different-project"
    different_project_dir.mkdir()

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=different_project_dir)
    # Address with provider but no host name implies new host
    address = AgentAddress(provider_name=ProviderInstanceName("docker"))
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
    )

    with pytest.raises(UserInputError, match="Project mismatch"):
        _parse_project_name(source_location, opts, address, temp_mng_ctx)


def test_parse_project_name_raises_on_mismatch_with_new_host_and_source_host(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """Raises UserInputError when source host project differs from local and creating a new host."""
    different_project_dir = tmp_path / "another-different-project"
    different_project_dir.mkdir()

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=different_project_dir)
    # Address with --new-host flag and a provider
    address = AgentAddress(
        host_name=HostName("myhost"),
        provider_name=ProviderInstanceName("modal"),
    )
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_host, "some-host"),
        to_update(default_create_cli_opts.field_ref().new_host, True),
    )

    with pytest.raises(UserInputError, match="Project mismatch"):
        _parse_project_name(source_location, opts, address, temp_mng_ctx)


def test_parse_project_name_no_error_without_new_host(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """No error when source project differs but no new host is being created."""
    different_project_dir = tmp_path / "yet-another-project"
    different_project_dir.mkdir()

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=different_project_dir)
    # Address targets existing host (not new)
    address = AgentAddress(host_name=HostName("myhost"))
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
    )

    # Should not raise - no new host means project tag doesn't matter
    result = _parse_project_name(source_location, opts, address, temp_mng_ctx)

    assert result == "yet-another-project"


def test_parse_project_name_no_error_without_external_source(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """No error when creating a new host without an external source reference."""
    some_dir = tmp_path / "some-project"
    some_dir.mkdir()

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=some_dir)
    # Address implies new host (provider only, no host name)
    address = AgentAddress(provider_name=ProviderInstanceName("docker"))

    # Should not raise - no source_agent/source_host means no external source
    result = _parse_project_name(source_location, opts=default_create_cli_opts, address=address, mng_ctx=temp_mng_ctx)

    assert result == "some-project"


# =============================================================================
# Tests for _split_cli_args
# =============================================================================


def test_split_cli_args_splits_space_separated_flag_and_value() -> None:
    """Regression: -b "--cpu 16" should split into ["--cpu", "16"]."""
    result = _split_cli_args(("--cpu 16", "--memory 16"))

    assert result == ["--cpu", "16", "--memory", "16"]


def test_split_cli_args_preserves_key_value_format() -> None:
    """Simple key=value args should pass through unchanged."""
    result = _split_cli_args(("cpu=16", "--memory=16"))

    assert result == ["cpu=16", "--memory=16"]


def test_split_cli_args_preserves_separate_flag_and_value() -> None:
    """Already-separate --flag and value args should pass through unchanged."""
    result = _split_cli_args(("--cpu", "16"))

    assert result == ["--cpu", "16"]


def test_split_cli_args_empty() -> None:
    """Empty input should produce empty output."""
    assert _split_cli_args(()) == []


# =============================================================================
# Tests for --label option in _parse_agent_opts
# =============================================================================


def test_parse_agent_opts_includes_labels(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """--label KEY=VALUE options should be parsed into label_options.labels."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().label, ("project=mng", "env=prod")),
    )

    result, _ = _parse_agent_opts(
        opts=opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.label_options.labels == {"project": "mng", "env": "prod"}


def test_parse_agent_opts_label_invalid_format_raises(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """--label without = should raise UserInputError."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().label, ("invalid-no-equals",)),
    )

    with pytest.raises(UserInputError, match="KEY=VALUE"):
        _parse_agent_opts(
            opts=opts,
            address=AgentAddress(),
            initial_message=None,
            source_location=source_location,
            mng_ctx=temp_mng_ctx,
        )


def test_parse_agent_opts_empty_labels_by_default(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Without --label, label_options.labels should be empty."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)

    result, _ = _parse_agent_opts(
        opts=default_create_cli_opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.label_options.labels == {}


def test_parse_agent_opts_with_agent_id(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """--id should be parsed into id field."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    explicit_id = AgentId()
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().id, str(explicit_id)),
    )

    result, _ = _parse_agent_opts(
        opts=opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.agent_id == explicit_id


def test_parse_agent_opts_agent_id_none_by_default(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Without --id, id should be None (auto-generated later)."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)

    result, _ = _parse_agent_opts(
        opts=default_create_cli_opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.agent_id is None


def test_parse_agent_opts_conflicting_type_and_positional_raises(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Specifying both --type and positional agent type with different values should raise."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().type, "claude"),
        to_update(default_create_cli_opts.field_ref().positional_agent_type, "codex"),
    )

    with pytest.raises(UserInputError, match="Conflicting agent types"):
        _parse_agent_opts(
            opts=opts,
            address=AgentAddress(),
            initial_message=None,
            source_location=source_location,
            mng_ctx=temp_mng_ctx,
        )


def test_parse_agent_opts_matching_type_and_positional_ok(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Specifying both --type and positional with the same value should not raise."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().type, "claude"),
        to_update(default_create_cli_opts.field_ref().positional_agent_type, "claude"),
    )

    result, _ = _parse_agent_opts(
        opts=opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.agent_type is not None
    assert str(result.agent_type) == "claude"


# =============================================================================
# Tests for _parse_branch_flag
# =============================================================================


def test_parse_branch_flag_base_only() -> None:
    """A branch spec with no colon means base branch only, no new branch."""
    base, new, has_explicit_base = _parse_branch_flag("main", AgentName("my-agent"))

    assert base == "main"
    assert new is None
    assert has_explicit_base is True


def test_parse_branch_flag_base_and_new() -> None:
    """BASE:NEW creates a new branch from the base."""
    base, new, has_explicit_base = _parse_branch_flag("main:feature", AgentName("my-agent"))

    assert base == "main"
    assert new == "feature"
    assert has_explicit_base is True


def test_parse_branch_flag_base_and_wildcard() -> None:
    """Wildcard * in NEW is replaced by the agent name."""
    base, new, has_explicit_base = _parse_branch_flag("main:mng/*", AgentName("my-agent"))

    assert base == "main"
    assert new == "mng/my-agent"
    assert has_explicit_base is True


def test_parse_branch_flag_empty_base_with_new() -> None:
    """Empty base (colon prefix) defaults base to None (current branch)."""
    base, new, has_explicit_base = _parse_branch_flag(":feature", AgentName("my-agent"))

    assert base is None
    assert new == "feature"
    assert has_explicit_base is False


def test_parse_branch_flag_empty_base_with_wildcard() -> None:
    """Default format :mng/* uses current branch and auto-generates name."""
    base, new, has_explicit_base = _parse_branch_flag(":mng/*", AgentName("my-agent"))

    assert base is None
    assert new == "mng/my-agent"
    assert has_explicit_base is False


def test_parse_branch_flag_empty_new_uses_default() -> None:
    """Empty NEW after colon (e.g. 'main:') falls back to default pattern."""
    base, new, has_explicit_base = _parse_branch_flag("main:", AgentName("my-agent"))

    assert base == "main"
    assert new == "mng/my-agent"
    assert has_explicit_base is True


def test_parse_branch_flag_just_colon_uses_default() -> None:
    """Just ':' means current branch with default new branch pattern."""
    base, new, has_explicit_base = _parse_branch_flag(":", AgentName("my-agent"))

    assert base is None
    assert new == "mng/my-agent"
    assert has_explicit_base is False


def test_parse_branch_flag_multiple_wildcards_raises() -> None:
    """More than one * in NEW raises an error."""
    with pytest.raises(UserInputError, match="at most one"):
        _parse_branch_flag("main:mng/*-*", AgentName("my-agent"))


def test_parse_branch_flag_empty_string() -> None:
    """Empty string means no base branch and no new branch."""
    base, new, has_explicit_base = _parse_branch_flag("", AgentName("my-agent"))

    assert base is None
    assert new is None
    assert has_explicit_base is False


def test_parse_branch_flag_new_without_wildcard() -> None:
    """NEW without wildcard uses the exact name."""
    base, new, has_explicit_base = _parse_branch_flag(":my-exact-branch", AgentName("ignored"))

    assert base is None
    assert new == "my-exact-branch"
    assert has_explicit_base is False


# =============================================================================
# Tests for parse_agent_address
# =============================================================================


def test_parse_agent_address_empty_string() -> None:
    """Empty string produces an address with all None fields."""
    result = parse_agent_address("")

    assert result.agent_name is None
    assert result.host_name is None
    assert result.provider_name is None


def test_parse_agent_address_simple_name() -> None:
    """A simple name with no @ produces just an agent name."""
    result = parse_agent_address("my-agent")

    assert result.agent_name == AgentName("my-agent")
    assert result.host_name is None
    assert result.provider_name is None


def test_parse_agent_address_name_and_host() -> None:
    """NAME@HOST produces agent name and host name."""
    result = parse_agent_address("my-agent@myhost")

    assert result.agent_name == AgentName("my-agent")
    assert result.host_name == HostName("myhost")
    assert result.provider_name is None


def test_parse_agent_address_name_host_and_provider() -> None:
    """NAME@HOST.PROVIDER produces all three components."""
    result = parse_agent_address("my-agent@myhost.modal")

    assert result.agent_name == AgentName("my-agent")
    assert result.host_name == HostName("myhost")
    assert result.provider_name == ProviderInstanceName("modal")


def test_parse_agent_address_name_and_provider_only() -> None:
    """NAME@.PROVIDER produces agent name and provider (implies new host)."""
    result = parse_agent_address("my-agent@.modal")

    assert result.agent_name == AgentName("my-agent")
    assert result.host_name is None
    assert result.provider_name == ProviderInstanceName("modal")


def test_parse_agent_address_no_name_with_host_and_provider() -> None:
    """@HOST.PROVIDER produces host and provider, no agent name."""
    result = parse_agent_address("@myhost.modal")

    assert result.agent_name is None
    assert result.host_name == HostName("myhost")
    assert result.provider_name == ProviderInstanceName("modal")


def test_parse_agent_address_no_name_with_provider_only() -> None:
    """@.PROVIDER produces just provider (implies new host, auto-generate name)."""
    result = parse_agent_address("@.docker")

    assert result.agent_name is None
    assert result.host_name is None
    assert result.provider_name == ProviderInstanceName("docker")


def test_parse_agent_address_trailing_at_ignored() -> None:
    """NAME@ is treated as just NAME (trailing @ with no host)."""
    result = parse_agent_address("my-agent@")

    assert result.agent_name == AgentName("my-agent")
    assert result.host_name is None
    assert result.provider_name is None
    assert result.has_host_component is False


def test_parse_agent_address_has_host_component() -> None:
    """has_host_component is True when any host info is present."""
    assert parse_agent_address("foo").has_host_component is False
    assert parse_agent_address("foo@host").has_host_component is True
    assert parse_agent_address("foo@.modal").has_host_component is True
    assert parse_agent_address("foo@host.modal").has_host_component is True


def test_is_creating_new_host() -> None:
    """_is_creating_new_host reflects both address and flag."""
    # Implied new host (no host name, has provider)
    addr = parse_agent_address("foo@.modal")
    assert _is_creating_new_host(addr, new_host_flag=False) is True
    assert _is_creating_new_host(addr, new_host_flag=True) is True

    # Existing host (has host name)
    addr = parse_agent_address("foo@myhost.modal")
    assert _is_creating_new_host(addr, new_host_flag=False) is False
    assert _is_creating_new_host(addr, new_host_flag=True) is True

    # No host component at all
    addr = parse_agent_address("foo")
    assert _is_creating_new_host(addr, new_host_flag=False) is False


def test_parse_agent_address_rejects_multiple_dots() -> None:
    """Addresses with more than one dot in the host part are invalid."""
    with pytest.raises(UserInputError, match="more than one dot"):
        parse_agent_address("foo@host.provider.extra")

    with pytest.raises(UserInputError, match="more than one dot"):
        parse_agent_address("foo@a.b.c")

    with pytest.raises(UserInputError, match="more than one dot"):
        parse_agent_address("@host.provider.extra")


def test_parse_agent_address_trailing_dot_means_host_only() -> None:
    """A trailing dot 'host.' means host name with no provider."""
    result = parse_agent_address("foo@host.")

    assert result.agent_name == AgentName("foo")
    assert result.host_name == HostName("host")
    assert result.provider_name is None


def test_parse_agent_address_bare_dot_means_nothing() -> None:
    """'@.' means no host and no provider (both parts empty)."""
    result = parse_agent_address("foo@.")

    assert result.agent_name == AgentName("foo")
    assert result.host_name is None
    assert result.provider_name is None


# =============================================================================
# Tests for positional / --name mutual exclusivity
# =============================================================================


def test_create_rejects_positional_and_name_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Providing both a positional address and --name should fail."""
    result = cli_runner.invoke(
        create,
        ["my-agent", "--name", "other-agent", "--command", "true", "--no-connect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot specify both" in result.output


@pytest.mark.tmux
def test_create_accepts_name_flag_alone(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--name alone (no positional) should work for specifying the agent address."""
    result = cli_runner.invoke(
        create,
        ["--name", "@.local", "--command", "true", "--no-connect", "--source-path", str(temp_work_dir)],
        obj=plugin_manager,
    )

    assert result.exit_code == 0


# =============================================================================
# Tests for --provider flag merge/conflict logic
# =============================================================================


@pytest.mark.tmux
def test_create_provider_flag_sets_provider(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--provider without an address provider should be accepted."""
    result = cli_runner.invoke(
        create,
        ["my-agent", "--provider", "local", "--command", "true", "--no-connect", "--source-path", str(temp_work_dir)],
        obj=plugin_manager,
    )

    assert result.exit_code == 0


def test_create_provider_flag_conflicts_with_address_provider(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--provider that conflicts with the address provider should abort."""
    result = cli_runner.invoke(
        create,
        ["my-agent@.modal", "--provider", "docker", "--command", "true", "--no-connect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Conflicting providers" in result.output


@pytest.mark.tmux
def test_create_provider_flag_redundant_with_address_is_ok(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--provider matching the address provider should succeed (redundant but not conflicting)."""
    result = cli_runner.invoke(
        create,
        [
            "my-agent@.local",
            "--provider",
            "local",
            "--command",
            "true",
            "--no-connect",
            "--source-path",
            str(temp_work_dir),
        ],
        obj=plugin_manager,
    )

    assert result.exit_code == 0
