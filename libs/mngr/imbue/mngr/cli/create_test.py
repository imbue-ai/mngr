"""Tests for create module helper functions."""

import subprocess
from pathlib import Path
from typing import Any
from typing import cast

import click
import pluggy
import pytest
import tomlkit
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.agent_addr import AgentAddress
from imbue.mngr.api.agent_addr import parse_agent_address
from imbue.mngr.api.find import ResolvedSource
from imbue.mngr.cli.create import _AutoLabels
from imbue.mngr.cli.create import _CreateCommand
from imbue.mngr.cli.create import _RECOVERED_MESSAGE_FILENAME
from imbue.mngr.cli.create import _apply_host_labels
from imbue.mngr.cli.create import _check_source_does_not_contain_state_dir
from imbue.mngr.cli.create import _editor_cleanup_scope
from imbue.mngr.cli.create import _get_source_remote_url
from imbue.mngr.cli.create import _is_creating_new_host
from imbue.mngr.cli.create import _parse_agent_opts
from imbue.mngr.cli.create import _parse_branch_flag
from imbue.mngr.cli.create import _parse_host_lifecycle_options
from imbue.mngr.cli.create import _parse_project_name
from imbue.mngr.cli.create import _parse_target_host
from imbue.mngr.cli.create import _rescue_editor_content
from imbue.mngr.cli.create import _resolve_agent_type_name
from imbue.mngr.cli.create import _resolve_initial_message_content
from imbue.mngr.cli.create import _resolve_source_location
from imbue.mngr.cli.create import _resolve_target_host
from imbue.mngr.cli.create import _split_address_and_target_path
from imbue.mngr.cli.create import _split_cli_args
from imbue.mngr.cli.create import _try_reuse_existing_agent
from imbue.mngr.cli.create import create
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.editor import EditorSession
from imbue.mngr.utils.logging import LoggingSuppressor
from imbue.mngr.utils.toml_config import load_config_file_tomlkit
from imbue.mngr.utils.toml_config import save_config_file


def _write_agent_type_command_to_settings(settings_path: Path, type_name: str, command: str) -> None:
    """Register ``type_name`` with ``command`` in ``settings.toml`` for tests.

    Inlines the load / setdefault("agent_types") / save dance so tests that
    declare a lightweight agent type do not need a top-level helper.
    """
    settings_doc = load_config_file_tomlkit(settings_path)
    agent_types = settings_doc.setdefault("agent_types", tomlkit.table())
    type_table = tomlkit.table()
    type_table["command"] = command
    agent_types[type_name] = type_table
    save_config_file(settings_path, settings_doc)


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


def test_try_reuse_existing_agent_no_agents_found(temp_mngr_ctx: MngrContext) -> None:
    """Returns None when no agents match the name."""
    result = _try_reuse_existing_agent(
        agent_name=AgentName("nonexistent"),
        provider_name=None,
        target_host_ref=None,
        mngr_ctx=temp_mngr_ctx,
        agent_and_host_loader=lambda: {},
    )

    assert result is None


def test_try_reuse_existing_agent_no_matching_name(temp_mngr_ctx: MngrContext) -> None:
    """Returns None when agents exist but none match the name."""
    host_ref = _make_discovered_host()
    agent_ref = _make_discovered_agent(agent_name="other-agent")

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=None,
        target_host_ref=None,
        mngr_ctx=temp_mngr_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


def test_try_reuse_existing_agent_filters_by_provider(temp_mngr_ctx: MngrContext) -> None:
    """Returns None when agent exists but on different provider."""
    host_ref = _make_discovered_host(provider="modal")
    agent_ref = _make_discovered_agent(agent_name="test-agent", provider="modal")

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
        target_host_ref=None,
        mngr_ctx=temp_mngr_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


def test_try_reuse_existing_agent_filters_by_host(temp_mngr_ctx: MngrContext) -> None:
    """Returns None when agent exists but on different host."""
    host_ref = _make_discovered_host(host_id=TEST_HOST_ID_1)
    agent_ref = _make_discovered_agent(agent_name="test-agent", host_id=TEST_HOST_ID_1)

    target_host_ref = _make_discovered_host(host_id=TEST_HOST_ID_2)

    result = _try_reuse_existing_agent(
        agent_name=AgentName("test-agent"),
        provider_name=None,
        target_host_ref=target_host_ref,
        mngr_ctx=temp_mngr_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


# -- Tests using real local provider infrastructure --


@pytest.mark.tmux
def test_try_reuse_existing_agent_found_and_started(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Returns (agent, host) when agent is found and started."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

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
            mngr_ctx=temp_mngr_ctx,
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
    temp_mngr_ctx: MngrContext,
) -> None:
    """Returns None when agent reference exists but agent not found on online host."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

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
        mngr_ctx=temp_mngr_ctx,
        agent_and_host_loader=lambda: {host_ref: [agent_ref]},
    )

    assert result is None


# =============================================================================
# Tests for _resolve_source_location and _resolve_target_host with is_start_desired
# =============================================================================


def test_resolve_source_location_with_auto_start_enabled(
    default_create_cli_opts: CreateCliOptions,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """_resolve_source_location returns an online host when is_start_desired=True."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source, f":{temp_work_dir}"),
    )

    result = _resolve_source_location(
        opts,
        agent_and_host_loader=lambda: {},
        mngr_ctx=temp_mngr_ctx,
        is_start_desired=True,
    )

    assert isinstance(result.location.host, OnlineHostInterface)
    assert result.location.path == temp_work_dir
    assert result.agent is None


def test_resolve_target_host_with_auto_start_enabled(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_resolve_target_host returns an online host when target is None and is_start_desired=True."""
    result = _resolve_target_host(
        target_host=None,
        mngr_ctx=temp_mngr_ctx,
        is_start_desired=True,
    )

    assert isinstance(result, OnlineHostInterface)


def test_resolve_target_host_with_host_reference(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """_resolve_target_host resolves a DiscoveredHost to an online host."""
    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_provider.host_id,
        host_name=HostName(LOCAL_HOST_NAME),
    )

    result = _resolve_target_host(
        target_host=host_ref,
        mngr_ctx=temp_mngr_ctx,
        is_start_desired=True,
    )

    assert isinstance(result, OnlineHostInterface)


# =============================================================================
# Tests for _parse_project_name
# =============================================================================


def test_parse_project_name_returns_explicit_project(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """When --project is specified, return it directly."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    resolved = ResolvedSource(location=HostLocation(host=local_host, path=temp_work_dir))
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().project, "explicit-project"),
    )

    result = _parse_project_name(resolved, opts, remote_url=None)

    assert result == "explicit-project"


def test_parse_project_name_inherits_from_source_agent(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When source agent has a project label, inherit it."""
    some_dir = tmp_path / "local-folder"
    some_dir.mkdir()
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    resolved = ResolvedSource(
        location=HostLocation(host=local_host, path=some_dir),
        agent=DiscoveredAgent(
            host_id=local_host.id,
            agent_id=AgentId("agent-00000000000000000000000000000001"),
            agent_name=AgentName("source-agent"),
            provider_name=ProviderInstanceName("local"),
            certified_data={"labels": {"project": "inherited-project"}},
        ),
    )

    result = _parse_project_name(resolved, default_create_cli_opts, remote_url=None)

    assert result == "inherited-project"


def test_parse_project_name_derives_from_remote_url(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When remote URL is available, derive project name from it."""
    some_dir = tmp_path / "local-folder"
    some_dir.mkdir()
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    resolved = ResolvedSource(location=HostLocation(host=local_host, path=some_dir))

    result = _parse_project_name(resolved, default_create_cli_opts, remote_url="https://github.com/owner/my-repo.git")

    assert result == "my-repo"


def test_parse_project_name_falls_back_to_folder_name(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When no remote URL, fall back to the source directory name."""
    some_dir = tmp_path / "some-project"
    some_dir.mkdir()
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    resolved = ResolvedSource(location=HostLocation(host=local_host, path=some_dir))

    result = _parse_project_name(resolved, default_create_cli_opts, remote_url=None)

    assert result == "some-project"


# =============================================================================
# Tests for _get_source_remote_url
# =============================================================================


def test_get_source_remote_url_returns_url_when_remote_exists(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When source location has a git repo with a remote, return the remote URL."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/my-repo.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=repo_dir)

    result = _get_source_remote_url(source_location)

    assert result == "https://github.com/owner/my-repo.git"


def test_get_source_remote_url_returns_none_when_no_remote(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When git repo has no remote, return None."""
    repo_dir = tmp_path / "no-remote"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=repo_dir)

    result = _get_source_remote_url(source_location)

    assert result is None


def test_get_source_remote_url_returns_none_when_no_git(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When source path is not a git repo, return None."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=plain_dir)

    result = _get_source_remote_url(source_location)

    assert result is None


# =============================================================================
# Tests for _AutoLabels
# =============================================================================


def test_auto_labels_dump_includes_remote_when_set() -> None:
    """model_dump includes both project and remote when remote is set."""
    meta = _AutoLabels(project="my-project", remote="https://github.com/owner/my-project.git")

    assert meta.model_dump(exclude_none=True) == {
        "project": "my-project",
        "remote": "https://github.com/owner/my-project.git",
    }


def test_auto_labels_dump_excludes_remote_when_none() -> None:
    """model_dump omits remote when it is None."""
    meta = _AutoLabels(project="my-project")

    assert meta.model_dump(exclude_none=True) == {"project": "my-project"}


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
# Tests for _resolve_agent_type_name (shared resolution logic)
# =============================================================================


def test_resolve_agent_type_name_type_flag_wins() -> None:
    """--type flag takes precedence over positional."""
    assert _resolve_agent_type_name("headless_command", "claude") == "headless_command"


def test_resolve_agent_type_name_positional_fallback() -> None:
    """Positional arg used when --type is None."""
    assert _resolve_agent_type_name(None, "headless_claude") == "headless_claude"


def test_resolve_agent_type_name_all_none() -> None:
    """All None returns None (default to claude)."""
    assert _resolve_agent_type_name(None, None) is None


# =============================================================================
# Tests for _resolve_initial_message_content (shared between headless + non-headless)
# =============================================================================


def test_resolve_initial_message_content_from_message(default_create_cli_opts: CreateCliOptions) -> None:
    """--message is returned verbatim."""
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().message, "do the thing"),
    )
    assert _resolve_initial_message_content(opts) == "do the thing"


def test_resolve_initial_message_content_from_file(default_create_cli_opts: CreateCliOptions, tmp_path: Path) -> None:
    """--message-file contents are returned."""
    message_path = tmp_path / "msg.txt"
    message_path.write_text("from file")
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().message_file, str(message_path)),
    )
    assert _resolve_initial_message_content(opts) == "from file"


def test_resolve_initial_message_content_none(default_create_cli_opts: CreateCliOptions) -> None:
    """Neither flag set returns None."""
    assert _resolve_initial_message_content(default_create_cli_opts) is None


def test_resolve_initial_message_content_rejects_both(
    default_create_cli_opts: CreateCliOptions, tmp_path: Path
) -> None:
    """--message and --message-file together raise UserInputError."""
    message_path = tmp_path / "msg.txt"
    message_path.write_text("from file")
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().message, "inline"),
        to_update(default_create_cli_opts.field_ref().message_file, str(message_path)),
    )
    with pytest.raises(UserInputError, match="Cannot provide both --message and --message-file"):
        _resolve_initial_message_content(opts)


# =============================================================================
# Tests for the headless CLI flow (create --foreground)
# =============================================================================


@pytest.mark.tmux
def test_create_headless_streams_output(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Creating a headless_command agent with --foreground should stream output.

    Registers a custom headless_command-based agent type with a specific command
    via settings.toml. Uses an explicit --source + --transfer=none to avoid
    depending on being inside a git repo and to skip transfer (the shared path
    would otherwise try to rsync the source dir, which is slow and unnecessary
    for a one-line ``echo`` agent).
    """
    profile_dir = get_or_create_profile_dir(temp_host_dir)
    _write_agent_type_command_to_settings(
        profile_dir / "settings.toml", "headless_command", "echo headless-test-output"
    )
    source_dir = tmp_path / "headless-src"
    source_dir.mkdir()
    result = cli_runner.invoke(
        create,
        [
            "--type",
            "headless_command",
            "--foreground",
            "--source",
            str(source_dir),
            "--transfer",
            "none",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    assert "headless-test-output" in result.output


@pytest.mark.tmux
def test_create_headless_with_message_does_not_raise(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Passing --message on the headless path must not blow up in api_create.

    Headless agents cannot receive a message via wait_for_ready_signal +
    send_message (both raise), so api_create must take the headless branch
    and deliver the prompt through stage_initial_message instead. The
    agent command here (a plain ``echo``) ignores the prompt file
    (headless_command has no prompt semantics); the test is purely
    checking that the flow completes when --message is supplied.
    """
    profile_dir = get_or_create_profile_dir(temp_host_dir)
    _write_agent_type_command_to_settings(
        profile_dir / "settings.toml", "headless_command", "echo headless-test-output"
    )
    source_dir = tmp_path / "headless-src"
    source_dir.mkdir()
    result = cli_runner.invoke(
        create,
        [
            "--type",
            "headless_command",
            "--foreground",
            "--source",
            str(source_dir),
            "--transfer",
            "none",
            "--message",
            "user message body",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    assert "headless-test-output" in result.output


# =============================================================================
# Tests for incompatible flag rejection on the headless path
# =============================================================================


def test_create_headless_rejects_incompatible_flags(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Headless agent types should reject flags that don't apply to the headless flow.

    Uses --reconnect, which is specific to the post-create connect/attach phase
    that headless skips.
    """
    result = cli_runner.invoke(
        create,
        ["--type", "headless_command", "--foreground", "--reconnect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "does not support" in result.output
    assert "--reconnect" in result.output


def test_create_headless_rejects_explicit_connect(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--connect contradicts headless semantics and should be rejected."""
    result = cli_runner.invoke(
        create,
        ["--type", "headless_command", "--foreground", "--connect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--connect" in result.output
    assert "does not support" in result.output


def test_create_headless_allows_no_connect(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--no-connect is redundant with headless (which never connects) and should be allowed.

    Pairs --no-connect with --reconnect (still incompatible) so the validator
    runs and we can confirm the incompatibility listing mentions --reconnect
    but not --connect/--no-connect.
    """
    result = cli_runner.invoke(
        create,
        ["--type", "headless_command", "--foreground", "--no-connect", "--reconnect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--reconnect" in result.output
    assert "--connect/" not in result.output
    assert "--no-connect" not in result.output


def test_create_headless_rejects_multiple_incompatible_flags(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Error message should list all incompatible flags that were explicitly set."""
    result = cli_runner.invoke(
        create,
        [
            "--type",
            "headless_command",
            "--foreground",
            "--reconnect",
            "--reuse",
            "--start-on-boot",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--reconnect" in result.output
    assert "--reuse" in result.output
    assert "--start-on-boot" in result.output


def test_create_headless_rejects_conflicting_positional_and_type_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Conflicting positional agent type and --type flag should raise even for headless types."""
    result = cli_runner.invoke(
        create,
        ["my-agent", "headless_command", "--type", "headless_claude"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Conflicting agent types" in result.output


# =============================================================================
# Tests for --foreground flag
# =============================================================================


def test_create_headless_without_foreground_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Headless agent types require --foreground."""
    result = cli_runner.invoke(
        create,
        ["--type", "headless_command"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--foreground" in result.output
    assert "headless" in result.output.lower()


def test_create_foreground_with_non_headless_type_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--foreground with a non-headless agent type should be rejected."""
    result = cli_runner.invoke(
        create,
        ["--type", "claude", "--foreground"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--foreground" in result.output
    assert "not headless" in result.output


def test_create_foreground_without_type_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--foreground without any agent type (default claude) should be rejected."""
    result = cli_runner.invoke(
        create,
        ["--foreground"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--foreground" in result.output


# =============================================================================
# Tests for shared source/transfer/git handling on the headless path
# =============================================================================


@pytest.mark.tmux
def test_create_headless_with_source_and_transfer_none_runs_in_place(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Headless with --source and --transfer=none should run the agent in the given directory.

    Headless shares the non-headless source / transfer handling: the
    default transfer strategy creates a worktree (or rsyncs) rather than
    running in-place. ``--transfer=none`` is the explicit opt-in to
    in-place. Uses a ``pwd`` command so the streamed output contains the
    work directory path.
    """
    profile_dir = get_or_create_profile_dir(temp_host_dir)
    _write_agent_type_command_to_settings(profile_dir / "settings.toml", "headless_command", "pwd")
    # Use a nested dir so the source-must-not-contain-state-dir check (which
    # scans for ``.mngr/``) does not fire against the shared pytest tmp root.
    source_dir = tmp_path / "headless-src"
    source_dir.mkdir()
    # Canonicalize before asserting: on macOS `/var` is a symlink to
    # `/private/var`, so pytest's ``tmp_path`` string and the ``pwd`` output
    # can disagree on the ``/private`` prefix even though they reference the
    # same directory.
    resolved_source_dir = source_dir.resolve()
    result = cli_runner.invoke(
        create,
        [
            "--type",
            "headless_command",
            "--foreground",
            "--source",
            str(source_dir),
            "--transfer",
            "none",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    assert str(resolved_source_dir) in result.output


# =============================================================================
# Tests for _apply_host_labels
# =============================================================================
#
# _create_agent calls _apply_host_labels on resolved online hosts so that
# --host-label KEY=VALUE entries are honored for both headless and
# interactive create (both for existing/local hosts and as a second,
# idempotent application on newly-created hosts). These tests pin down
# that behavior so a refactor cannot silently skip the host-label
# application on any of those paths.


def test_apply_host_labels_adds_tags_to_local_host(
    local_provider: LocalProviderInstance,
) -> None:
    """KEY=VALUE host labels should be applied as tags on the local host."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

    _apply_host_labels(local_host, ("env=prod", "team=infra"))

    tags = local_provider.get_host_tags(local_host)
    assert tags.get("env") == "prod"
    assert tags.get("team") == "infra"


def test_apply_host_labels_empty_tuple_is_noop(
    local_provider: LocalProviderInstance,
) -> None:
    """An empty label tuple should not touch the host's tags."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    before = dict(local_provider.get_host_tags(local_host))

    _apply_host_labels(local_host, ())

    assert local_provider.get_host_tags(local_host) == before


def test_apply_host_labels_strips_whitespace(
    local_provider: LocalProviderInstance,
) -> None:
    """Whitespace around KEY and VALUE should be stripped."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

    _apply_host_labels(local_host, ("  env  =  prod  ",))

    assert local_provider.get_host_tags(local_host).get("env") == "prod"


def test_apply_host_labels_raises_on_entries_without_equals(
    local_provider: LocalProviderInstance,
) -> None:
    """Labels without '=' must raise UserInputError.

    _parse_target_host raises UserInputError for missing '=' on the new-host
    branch. _apply_host_labels mirrors that validation so malformed entries
    cannot slip through on the existing-host or headless-create paths --
    silently dropping them would hide user mistakes.
    """
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

    with pytest.raises(UserInputError, match="KEY=VALUE"):
        _apply_host_labels(local_host, ("no-equals-here", "env=prod"))


# =============================================================================
# Tests for --label option in _parse_agent_opts
# =============================================================================


def test_parse_agent_opts_includes_labels(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """--label KEY=VALUE options should be parsed into label_options.labels."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=temp_work_dir)
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().label, ("project=mngr", "env=prod")),
    )

    result, _ = _parse_agent_opts(
        opts=opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mngr_ctx=temp_mngr_ctx,
    )

    assert result.label_options.labels == {"project": "mngr", "env": "prod"}


def test_parse_agent_opts_label_invalid_format_raises(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """--label without = should raise UserInputError."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
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
            mngr_ctx=temp_mngr_ctx,
        )


def test_parse_agent_opts_empty_labels_by_default(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Without --label, label_options.labels should be empty."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=temp_work_dir)

    result, _ = _parse_agent_opts(
        opts=default_create_cli_opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mngr_ctx=temp_mngr_ctx,
    )

    assert result.label_options.labels == {}


def test_parse_agent_opts_with_agent_id(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """--id should be parsed into id field."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
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
        mngr_ctx=temp_mngr_ctx,
    )

    assert result.agent_id == explicit_id


def test_parse_agent_opts_agent_id_none_by_default(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Without --id, id should be None (auto-generated later)."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
    source_location = HostLocation(host=local_host, path=temp_work_dir)

    result, _ = _parse_agent_opts(
        opts=default_create_cli_opts,
        address=AgentAddress(),
        initial_message=None,
        source_location=source_location,
        mngr_ctx=temp_mngr_ctx,
    )

    assert result.agent_id is None


def test_parse_agent_opts_matching_type_and_positional_ok(
    default_create_cli_opts: CreateCliOptions,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Specifying both --type and positional with the same value should not raise."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))
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
        mngr_ctx=temp_mngr_ctx,
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
    base, new, has_explicit_base = _parse_branch_flag("main:mngr/*", AgentName("my-agent"))

    assert base == "main"
    assert new == "mngr/my-agent"
    assert has_explicit_base is True


def test_parse_branch_flag_empty_base_with_new() -> None:
    """Empty base (colon prefix) defaults base to None (current branch)."""
    base, new, has_explicit_base = _parse_branch_flag(":feature", AgentName("my-agent"))

    assert base is None
    assert new == "feature"
    assert has_explicit_base is False


def test_parse_branch_flag_empty_base_with_wildcard() -> None:
    """Default format :mngr/* uses current branch and auto-generates name."""
    base, new, has_explicit_base = _parse_branch_flag(":mngr/*", AgentName("my-agent"))

    assert base is None
    assert new == "mngr/my-agent"
    assert has_explicit_base is False


def test_parse_branch_flag_empty_new_uses_default() -> None:
    """Empty NEW after colon (e.g. 'main:') falls back to default pattern."""
    base, new, has_explicit_base = _parse_branch_flag("main:", AgentName("my-agent"))

    assert base == "main"
    assert new == "mngr/my-agent"
    assert has_explicit_base is True


def test_parse_branch_flag_just_colon_uses_default() -> None:
    """Just ':' means current branch with default new branch pattern."""
    base, new, has_explicit_base = _parse_branch_flag(":", AgentName("my-agent"))

    assert base is None
    assert new == "mngr/my-agent"
    assert has_explicit_base is False


def test_parse_branch_flag_multiple_wildcards_raises() -> None:
    """More than one * in NEW raises an error."""
    with pytest.raises(UserInputError, match="at most one"):
        _parse_branch_flag("main:mngr/*-*", AgentName("my-agent"))


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


def test_parse_target_host_local_provider_uses_fixed_host(
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """_parse_target_host returns None (use fixed localhost) when provider is local."""
    address = parse_agent_address("foo@.local")
    lifecycle = HostLifecycleOptions()

    result = _parse_target_host(
        opts=default_create_cli_opts,
        address=address,
        agent_and_host_loader=lambda: {},
        lifecycle=lifecycle,
    )

    # None means "use the local provider's default host" in _resolve_target_host
    assert result is None


def test_parse_target_host_local_provider_with_new_host_flag(
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """_parse_target_host returns None for local provider even with --new-host flag."""
    address = parse_agent_address("foo@myhost.local")
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().new_host, True),
    )
    lifecycle = HostLifecycleOptions()

    result = _parse_target_host(
        opts=opts,
        address=address,
        agent_and_host_loader=lambda: {},
        lifecycle=lifecycle,
    )

    assert result is None


def test_parse_target_host_non_local_provider_creates_new_host(
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """_parse_target_host returns NewHostOptions for non-local providers."""
    address = parse_agent_address("foo@.modal")
    lifecycle = HostLifecycleOptions()

    result = _parse_target_host(
        opts=default_create_cli_opts,
        address=address,
        agent_and_host_loader=lambda: {},
        lifecycle=lifecycle,
    )

    assert isinstance(result, NewHostOptions)
    assert result.provider == ProviderInstanceName("modal")


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
# Tests for --update / --reuse validation
# =============================================================================


def test_create_rejects_update_without_reuse(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--update without --reuse should fail with a clear error."""
    result = cli_runner.invoke(
        create,
        ["my-agent", "--update", "--type", "command", "--no-connect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--update requires --reuse" in result.output


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
        ["my-agent", "--name", "other-agent", "--type", "command", "--no-connect"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot specify both" in result.output


def test_create_edit_message_error_not_swallowed(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Early errors with --edit-message must still be visible.

    LoggingSuppressor is enabled early when --edit-message is set. If an error
    occurs before the editor opens, the suppressor must be cleaned up so the
    error message is not swallowed and stdout/stderr are restored.
    """
    result = cli_runner.invoke(
        create,
        ["my-agent", "--name", "other-agent", "--type", "command", "--no-connect", "--edit-message"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot specify both" in result.output
    assert not LoggingSuppressor.is_suppressed()


@pytest.mark.tmux
def test_create_accepts_name_flag_alone(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--name alone (no positional) should work for specifying the agent address."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            "@.local",
            "--type",
            "command",
            "--no-connect",
            "--transfer=none",
            "--from",
            str(temp_work_dir),
            "--",
            "true",
        ],
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
        [
            "my-agent",
            "--provider",
            "local",
            "--type",
            "command",
            "--no-connect",
            "--transfer=none",
            "--from",
            str(temp_work_dir),
            "--",
            "true",
        ],
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
        ["my-agent@.modal", "--provider", "docker", "--type", "command", "--no-connect"],
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
            "--type",
            "command",
            "--no-connect",
            "--transfer=none",
            "--from",
            str(temp_work_dir),
            "--",
            "true",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code == 0


# =============================================================================
# Tests for _rescue_editor_content
# =============================================================================


def test_rescue_editor_content_saves_content_to_recovery_file(
    editor_recovery_dir: Path,
) -> None:
    """Test that _rescue_editor_content saves editor content to the recovery directory."""
    session = EditorSession.create(initial_content="important message to save")

    _rescue_editor_content(session, recovery_dir=editor_recovery_dir)

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert recovery_path.exists()
    assert recovery_path.read_text() == "important message to save"

    session.cleanup()


def test_rescue_editor_content_does_nothing_when_temp_file_missing(
    editor_recovery_dir: Path,
) -> None:
    """Test that _rescue_editor_content does nothing when the temp file is missing."""
    session = EditorSession.create(initial_content="some content")
    # Delete the temp file to simulate it being missing
    session.temp_file_path.unlink()

    _rescue_editor_content(session, recovery_dir=editor_recovery_dir)

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert not recovery_path.exists()

    session.cleanup()


def test_rescue_editor_content_does_nothing_when_content_is_empty(
    editor_recovery_dir: Path,
) -> None:
    """Test that _rescue_editor_content does nothing when the temp file is empty."""
    session = EditorSession.create()

    _rescue_editor_content(session, recovery_dir=editor_recovery_dir)

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert not recovery_path.exists()

    session.cleanup()


def test_rescue_editor_content_strips_trailing_whitespace(
    editor_recovery_dir: Path,
) -> None:
    """Test that _rescue_editor_content strips trailing whitespace."""
    session = EditorSession.create(initial_content="content with trailing space  \n\n")

    _rescue_editor_content(session, recovery_dir=editor_recovery_dir)

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert recovery_path.exists()
    assert recovery_path.read_text() == "content with trailing space"

    session.cleanup()


# =============================================================================
# Tests for _editor_cleanup_scope
# =============================================================================


def test_editor_cleanup_scope_rescues_content_on_exception(
    editor_recovery_dir: Path,
) -> None:
    """Test that _editor_cleanup_scope saves editor content when an exception occurs."""
    session = EditorSession.create(initial_content="do not lose this message")

    with pytest.raises(RuntimeError, match="simulated failure"):
        with _editor_cleanup_scope(session, recovery_dir=editor_recovery_dir):
            raise RuntimeError("simulated failure")

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert recovery_path.exists()
    assert recovery_path.read_text() == "do not lose this message"

    # Temp file should be cleaned up by the finally block
    assert not session.temp_file_path.exists()


def test_editor_cleanup_scope_does_not_rescue_on_success(
    editor_recovery_dir: Path,
) -> None:
    """Test that _editor_cleanup_scope does not create a recovery file on success."""
    session = EditorSession.create(initial_content="message content")

    with _editor_cleanup_scope(session, recovery_dir=editor_recovery_dir):
        pass

    recovery_path = editor_recovery_dir / _RECOVERED_MESSAGE_FILENAME
    assert not recovery_path.exists()

    # Temp file should still be cleaned up
    assert not session.temp_file_path.exists()


# =============================================================================
# Tests for _check_source_does_not_contain_state_dir
# =============================================================================


def test_check_source_does_not_contain_state_dir_raises_when_source_is_parent(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Raises when the source directory is a parent of the mngr state dir."""
    state_dir = temp_mngr_ctx.config.default_host_dir.expanduser().resolve()
    parent_of_state_dir = state_dir.parent

    with pytest.raises(UserInputError, match="contains the mngr state directory"):
        _check_source_does_not_contain_state_dir(parent_of_state_dir, temp_mngr_ctx)


def test_check_source_does_not_contain_state_dir_raises_when_source_is_state_dir(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Raises when the source directory IS the mngr state dir."""
    state_dir = temp_mngr_ctx.config.default_host_dir.expanduser().resolve()

    with pytest.raises(UserInputError, match="contains the mngr state directory"):
        _check_source_does_not_contain_state_dir(state_dir, temp_mngr_ctx)


def test_check_source_does_not_contain_state_dir_passes_for_sibling(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Does not raise when the source directory is a sibling of the state dir."""
    sibling_dir = tmp_path / "some-project"
    sibling_dir.mkdir()

    # Should not raise
    _check_source_does_not_contain_state_dir(sibling_dir, temp_mngr_ctx)


def test_check_source_does_not_contain_state_dir_passes_for_child_of_state_dir(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Does not raise when the source directory is inside the state dir (child)."""
    state_dir = temp_mngr_ctx.config.default_host_dir.expanduser().resolve()
    child_dir = state_dir / "agents" / "some-agent"
    child_dir.mkdir(parents=True, exist_ok=True)

    # Should not raise -- we only block the parent-contains-state-dir direction
    _check_source_does_not_contain_state_dir(child_dir, temp_mngr_ctx)


# =============================================================================
# Tests for _resolve_source_location without git repo
# =============================================================================


def test_resolve_source_location_raises_outside_git_repo(
    default_create_cli_opts: CreateCliOptions,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_source_location raises UserInputError when not in a git repo and no source specified."""
    # tmp_path is not a git repo, change cwd to it
    monkeypatch.chdir(tmp_path)

    with pytest.raises(UserInputError, match="Not inside a git repository"):
        _resolve_source_location(
            opts=default_create_cli_opts,
            agent_and_host_loader=lambda: {},
            mngr_ctx=temp_mngr_ctx,
            is_start_desired=True,
        )


# =============================================================================
# Tests for _split_address_and_target_path
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected_addr", "expected_path"),
    [
        pytest.param("foo", "foo", None, id="no_colon"),
        pytest.param("", "", None, id="empty_string"),
        pytest.param("foo:/tmp/work", "foo", Path("/tmp/work"), id="absolute_path"),
        pytest.param(":./rel/path", "", Path("./rel/path"), id="relative_path"),
        pytest.param("foo@host.modal:/root/work", "foo@host.modal", Path("/root/work"), id="full_address_with_path"),
        pytest.param(":/tmp/work", "", Path("/tmp/work"), id="path_only"),
        pytest.param("foo:", "foo", None, id="trailing_colon"),
    ],
)
def test_split_address_and_target_path(raw: str, expected_addr: str, expected_path: Path | None) -> None:
    """_split_address_and_target_path parses address and optional :PATH suffix."""
    addr, path = _split_address_and_target_path(raw)
    assert addr == expected_addr
    assert path == expected_path
