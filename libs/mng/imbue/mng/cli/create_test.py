"""Tests for create module helper functions."""

from pathlib import Path
from typing import cast

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mng.cli.create import CreateCliOptions
from imbue.mng.cli.create import _parse_agent_opts
from imbue.mng.cli.create import _parse_host_lifecycle_options
from imbue.mng.cli.create import _parse_project_name
from imbue.mng.cli.create import _resolve_source_location
from imbue.mng.cli.create import _resolve_target_host
from imbue.mng.cli.create import _split_cli_args
from imbue.mng.cli.create import _try_reuse_existing_agent
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance

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


def _make_host_ref(
    provider: str = "local", host_id: str = TEST_HOST_ID_1, host_name: str = "test-host"
) -> HostReference:
    return HostReference(
        provider_name=ProviderInstanceName(provider),
        host_id=HostId(host_id),
        host_name=HostName(host_name),
    )


def _make_agent_ref(
    agent_id: str = TEST_AGENT_ID_1,
    agent_name: str = "test-agent",
    host_id: str = TEST_HOST_ID_1,
    provider: str = "local",
) -> AgentReference:
    return AgentReference(
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
    host_ref = _make_host_ref()
    agent_ref = _make_agent_ref(agent_name="other-agent")

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
    host_ref = _make_host_ref(provider="modal")
    agent_ref = _make_agent_ref(agent_name="test-agent", provider="modal")

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
    host_ref = _make_host_ref(host_id=TEST_HOST_ID_1)
    agent_ref = _make_agent_ref(agent_name="test-agent", host_id=TEST_HOST_ID_1)

    target_host_ref = _make_host_ref(host_id=TEST_HOST_ID_2)

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
    host_ref = HostReference(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = AgentReference(
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
    host_ref = HostReference(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = AgentReference(
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

    result = _resolve_source_location(
        opts,
        agent_and_host_loader=lambda: {},
        mng_ctx=temp_mng_ctx,
        is_start_desired=True,
    )

    assert isinstance(result.host, OnlineHostInterface)
    assert result.path == temp_work_dir


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
    """_resolve_target_host resolves a HostReference to an online host."""
    host_ref = HostReference(
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
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().project, "explicit-project"),
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
        to_update(default_create_cli_opts.field_ref().new_host, "docker"),
    )

    result = _parse_project_name(source_location, opts, temp_mng_ctx)

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
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
        to_update(default_create_cli_opts.field_ref().new_host, "docker"),
    )

    with pytest.raises(UserInputError, match="Project mismatch"):
        _parse_project_name(source_location, opts, temp_mng_ctx)


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
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_host, "some-host"),
        to_update(default_create_cli_opts.field_ref().new_host, "modal"),
    )

    with pytest.raises(UserInputError, match="Project mismatch"):
        _parse_project_name(source_location, opts, temp_mng_ctx)


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
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().source_agent, "some-agent"),
    )

    # Should not raise - no new host means project tag doesn't matter
    result = _parse_project_name(source_location, opts, temp_mng_ctx)

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
    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().new_host, "docker"),
    )

    # Should not raise - no source_agent/source_host means no external source
    result = _parse_project_name(source_location, opts, temp_mng_ctx)

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

    result = _parse_agent_opts(
        opts=opts,
        initial_message=None,
        resume_message=None,
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
            initial_message=None,
            resume_message=None,
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

    result = _parse_agent_opts(
        opts=default_create_cli_opts,
        initial_message=None,
        resume_message=None,
        source_location=source_location,
        mng_ctx=temp_mng_ctx,
    )

    assert result.label_options.labels == {}
