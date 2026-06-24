from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.api.create import _create_new_host
from imbue.mngr.api.create import _generate_unique_host_name
from imbue.mngr.api.create import _run_post_host_create_commands
from imbue.mngr.api.create import _run_post_host_create_outer_commands
from imbue.mngr.api.create import _validate_session_adoption
from imbue.mngr.api.create import _write_host_env_vars
from imbue.mngr.api.create import create
from imbue.mngr.api.create import destroy_new_host_on_create_failure
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNameConflictError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE
from imbue.mngr.utils.testing import make_ctx_with_plugins


def test_write_host_env_vars_writes_explicit_env_vars(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """Test that _write_host_env_vars writes explicit env vars to the host env file."""
    environment = HostEnvironmentOptions(
        env_vars=(
            EnvVar(key="FOO", value="bar"),
            EnvVar(key="BAZ", value="qux"),
        ),
    )

    _write_host_env_vars(local_host, environment)

    host_env = local_host.get_env_vars()
    assert host_env["FOO"] == "bar"
    assert host_env["BAZ"] == "qux"


def test_write_host_env_vars_reads_env_files(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that _write_host_env_vars reads env files and writes to the host env file."""
    env_file = tmp_path / "test.env"
    env_file.write_text("FILE_VAR=from_file\nANOTHER=value\n")

    environment = HostEnvironmentOptions(
        env_files=(env_file,),
    )

    _write_host_env_vars(local_host, environment)

    host_env = local_host.get_env_vars()
    assert host_env["FILE_VAR"] == "from_file"
    assert host_env["ANOTHER"] == "value"


def test_write_host_env_vars_explicit_overrides_file(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that explicit env vars override values from env files."""
    env_file = tmp_path / "test.env"
    env_file.write_text("SHARED=from_file\nFILE_ONLY=present\n")

    environment = HostEnvironmentOptions(
        env_vars=(EnvVar(key="SHARED", value="from_explicit"),),
        env_files=(env_file,),
    )

    _write_host_env_vars(local_host, environment)

    host_env = local_host.get_env_vars()
    assert host_env["SHARED"] == "from_explicit"
    assert host_env["FILE_ONLY"] == "present"


def test_write_host_env_vars_skips_when_empty(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """Test that _write_host_env_vars does nothing when no env vars or files are specified."""
    environment = HostEnvironmentOptions()

    _write_host_env_vars(local_host, environment)

    # The host env file should not exist (no env vars written)
    host_env = local_host.get_env_vars()
    assert host_env == {}


# =============================================================================
# _run_post_host_create_commands Tests
# =============================================================================


def test_run_post_host_create_commands_no_op_on_empty_tuple(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """An empty commands tuple is a no-op (no exec, no error)."""
    _run_post_host_create_commands(local_host, ())


def test_run_post_host_create_commands_runs_each_command_in_order(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Each command runs in order; we observe order via append-to-file side effect."""
    marker = tmp_path / "order.txt"
    _run_post_host_create_commands(
        local_host,
        (
            CommandString(f"echo first >> {marker}"),
            CommandString(f"echo second >> {marker}"),
            CommandString(f"echo third >> {marker}"),
        ),
    )
    assert marker.read_text().splitlines() == ["first", "second", "third"]


def test_run_post_host_create_commands_raises_on_first_failure(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """A non-zero exit raises MngrError, and subsequent commands do not run."""
    marker = tmp_path / "after_failure.txt"
    with pytest.raises(MngrError, match="post-host-create command failed"):
        _run_post_host_create_commands(
            local_host,
            (
                CommandString("false"),
                CommandString(f"echo unreached >> {marker}"),
            ),
        )
    # The second command must not have executed.
    assert not marker.exists()


# =============================================================================
# _run_post_host_create_outer_commands Tests
# =============================================================================


def test_run_post_host_create_outer_commands_no_op_on_empty_tuple(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """An empty commands tuple is a no-op (the outer is never even opened)."""
    _run_post_host_create_outer_commands(local_host, ())


def test_run_post_host_create_outer_commands_raises_when_no_outer(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Configuring outer commands on a provider with no outer host is a misconfiguration -> raise."""
    marker = tmp_path / "outer_ran.txt"
    # Sanity: the local host has no outer.
    with local_host.outer_host() as outer:
        assert outer is None
    # The command can never run, so we must raise (not silently skip), and must not run it.
    with pytest.raises(MngrError, match="no outer host"):
        _run_post_host_create_outer_commands(local_host, (CommandString(f"touch {marker}"),))
    assert not marker.exists()


# =============================================================================
# resolve_target_host Tests
# =============================================================================


def test_resolve_target_host_with_existing_host(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """resolve_target_host should return the host directly when given an existing OnlineHostInterface."""
    assert isinstance(local_host, OnlineHostInterface)

    resolved = resolve_target_host(local_host, temp_mngr_ctx)
    assert resolved.id == local_host.id


# =============================================================================
# _generate_unique_host_name Tests
# =============================================================================


def test_generate_unique_host_name_avoids_existing_names(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """_generate_unique_host_name produces a name not already used by existing hosts.

    The local provider generates "localhost" every time and has one host named
    "localhost", so every attempt collides. This test uses the real COOLNAME
    style with a non-local-provider name generator that produces random names
    from a large pool, so collisions are effectively impossible.
    """
    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=None,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    # The local provider discovers one host ("localhost") and get_host_name
    # returns "localhost" every time (guaranteed collision). Override
    # get_host_name by using the base class implementation which generates
    # random names from a large pool -- no collision with "localhost".
    original_get_host_name = ProviderInstanceInterface.get_host_name
    test_provider_cls = type(
        "_TestProvider",
        (LocalProviderInstance,),
        {"get_host_name": lambda self, style: original_get_host_name(self, style)},
    )
    provider = test_provider_cls(
        name=local_provider.name,
        host_dir=local_provider.host_dir,
        mngr_ctx=local_provider.mngr_ctx,
    )
    result = _generate_unique_host_name(provider, target, temp_mngr_ctx)

    assert result != HostName("localhost")


def test_generate_unique_host_name_raises_after_exhausting_attempts(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """_generate_unique_host_name raises MngrError when all names collide.

    The local provider always generates "localhost" and has a host named
    "localhost", so every attempt collides forever.
    """
    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=None,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    with pytest.raises(MngrError, match="Failed to generate a unique host name"):
        _generate_unique_host_name(local_provider, target, temp_mngr_ctx)


def test_create_new_host_retries_on_name_conflict(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """resolve_target_host retries with a new name when create_host raises HostNameConflictError.

    Uses a provider subclass that raises HostNameConflictError on the first
    call then succeeds, to verify the retry loop in resolve_target_host.
    """
    create_count = 0
    original_create_host = LocalProviderInstance.create_host

    def create_host_that_conflicts_once(self: LocalProviderInstance, name: HostName, **kwargs: object) -> Host:
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            raise HostNameConflictError(self.name, name)
        return original_create_host(self, name=name, **kwargs)

    test_provider_cls = type(
        "_ConflictTestProvider",
        (LocalProviderInstance,),
        {
            "get_host_name": lambda self, style: HostName("localhost"),
            "create_host": create_host_that_conflicts_once,
        },
    )
    provider = test_provider_cls(
        name=local_provider.name,
        host_dir=local_provider.host_dir,
        mngr_ctx=local_provider.mngr_ctx,
    )

    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=None,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    # First call should raise HostNameConflictError
    with pytest.raises(HostNameConflictError):
        _create_new_host(provider, HostName("localhost"), target, temp_mngr_ctx)
    assert create_count == 1

    # Second call should succeed (the retry logic in resolve_target_host
    # would call _create_new_host again with a new name)
    result = _create_new_host(provider, HostName("localhost"), target, temp_mngr_ctx)
    assert create_count == 2
    assert isinstance(result, OnlineHostInterface)


def test_write_host_env_vars_later_env_file_overrides_earlier(
    local_host: Host,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """_write_host_env_vars should let later env files override earlier ones."""
    env_file_1 = tmp_path / "first.env"
    env_file_1.write_text("SHARED=from_first\nFIRST_ONLY=present\n")

    env_file_2 = tmp_path / "second.env"
    env_file_2.write_text("SHARED=from_second\nSECOND_ONLY=present\n")

    environment = HostEnvironmentOptions(
        env_files=(env_file_1, env_file_2),
    )

    _write_host_env_vars(local_host, environment)

    host_env = local_host.get_env_vars()
    assert host_env["SHARED"] == "from_second"
    assert host_env["FIRST_ONLY"] == "present"
    assert host_env["SECOND_ONLY"] == "present"


# =============================================================================
# destroy_new_host_on_create_failure -- a host we just created for a --new-host
# create must be torn down if a later step fails, so we never leak it (and, for
# non-idle-shutdown providers like imbue_cloud, its lease). Gated by the debug
# retain flag.
# =============================================================================

_RETAIN_FLAG = "MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE"


class _RecordingDestroyProvider(LocalProviderInstance):
    """LocalProviderInstance that records destroy_host calls instead of performing them."""

    destroyed_host_ids: list[HostId] = Field(default_factory=list)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        self.destroyed_host_ids.append(host if isinstance(host, HostId) else host.id)


def _make_recording_provider(local_provider: LocalProviderInstance) -> _RecordingDestroyProvider:
    return _RecordingDestroyProvider(
        name=local_provider.name,
        host_dir=local_provider.host_dir,
        mngr_ctx=local_provider.mngr_ctx,
    )


def test_destroy_new_host_on_create_failure_destroys_failed_new_host(
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """A failure inside the guard tears down the newly-created host and re-raises."""
    provider = _make_recording_provider(local_provider)
    with pytest.raises(ValueError):
        with destroy_new_host_on_create_failure(local_host, provider):
            raise ValueError("provisioning blew up")
    assert provider.destroyed_host_ids == [local_host.id]


@pytest.mark.allow_warnings(match=r"^Failed to destroy host .* after a failed create")
def test_destroy_new_host_on_create_failure_leaked_resource_does_not_mask_create_error(
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """A leaked-resource failure during rollback must not mask the original create error.

    The teardown runs in a finally; destroy_host now raises a CleanupFailedGroup when it
    leaves a resource behind. That group must be swallowed (logged) so the ValueError that
    actually caused the create to fail is what propagates -- otherwise the user sees a
    confusing cleanup error instead of the real cause.
    """

    class _LeakyDestroyProvider(LocalProviderInstance):
        def destroy_host(self, host: HostInterface | HostId) -> None:
            raise CleanupFailedGroup.from_failures(
                [
                    CleanupFailure(
                        category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                        message="container could not be removed",
                        host_id=local_host.id,
                    )
                ]
            )

    provider = _LeakyDestroyProvider(
        name=local_provider.name,
        host_dir=local_provider.host_dir,
        mngr_ctx=local_provider.mngr_ctx,
    )
    with pytest.raises(ValueError, match="provisioning blew up"):
        with destroy_new_host_on_create_failure(local_host, provider):
            raise ValueError("provisioning blew up")


def test_destroy_new_host_on_create_failure_is_noop_on_success(
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """A clean exit must not destroy the host."""
    provider = _make_recording_provider(local_provider)
    with destroy_new_host_on_create_failure(local_host, provider):
        pass
    assert provider.destroyed_host_ids == []


def test_destroy_new_host_on_create_failure_does_not_destroy_existing_host(local_host: Host) -> None:
    """provider=None means the caller already owned the host; never tear it down (just re-raise)."""
    with pytest.raises(ValueError):
        with destroy_new_host_on_create_failure(local_host, None):
            raise ValueError("boom")


def test_destroy_new_host_on_create_failure_retains_host_when_debug_flag_set(
    local_provider: LocalProviderInstance,
    local_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The debug retain flag suppresses the teardown so a failed host can be inspected."""
    provider = _make_recording_provider(local_provider)
    monkeypatch.setenv(_RETAIN_FLAG, "1")
    with pytest.raises(ValueError):
        with destroy_new_host_on_create_failure(local_host, provider):
            raise ValueError("boom")
    assert provider.destroyed_host_ids == []


# =============================================================================
# End-to-end: a --new-host create that fails at/before the initial-message send
# must tear the new host down (single continuous guard around the whole flow),
# unless the debug retain flag is set.
# =============================================================================


@contextmanager
def _injected_provider(
    name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    instance: LocalProviderInstance,
) -> Generator[None, None, None]:
    """Temporarily inject a provider instance into the provider cache.

    ``create`` resolves the new-host provider via ``get_provider_instance`` using
    the same ``mngr_ctx``; injecting here makes it use our recording provider so
    we can observe whether ``destroy_host`` is called on failure.
    """
    cache_key = (name, id(mngr_ctx))
    _instance_cache[cache_key] = instance
    try:
        yield
    finally:
        _instance_cache.pop(cache_key, None)


class _RaiseInOnHostCreated:
    """Plugin that raises in on_host_created.

    This fires for a --new-host create after ``create_host`` has already
    succeeded but before the initial message is sent, simulating any failure in
    that window.
    """

    @hookimpl
    def on_host_created(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        raise RuntimeError("simulated failure after host create, before message send")


def _make_create_agent_options() -> CreateAgentOptions:
    return CreateAgentOptions(
        name=AgentName("test-teardown-agent"),
        agent_type=AgentTypeName(PLACEHOLDER_AGENT_TYPE),
        command=CommandString("sleep 60"),
        transfer_mode=TransferMode.NONE,
        label_options=AgentLabelOptions(),
    )


def test_create_new_host_torn_down_when_failure_before_message_send(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """A new-host create that fails after create_host but before the message send tears the host down."""
    test_ctx = make_ctx_with_plugins(temp_mngr_ctx, [_RaiseInOnHostCreated()])
    provider = _make_recording_provider(local_provider)

    source_location = HostLocation(host=local_provider.create_host(HostName(LOCAL_HOST_NAME)), path=temp_work_dir)
    target_host = NewHostOptions(provider=ProviderInstanceName(LOCAL_PROVIDER_NAME), name=HostName(LOCAL_HOST_NAME))

    with _injected_provider(ProviderInstanceName(LOCAL_PROVIDER_NAME), test_ctx, provider):
        with pytest.raises(RuntimeError, match="simulated failure after host create"):
            create(
                source_location=source_location,
                target_host=target_host,
                agent_options=_make_create_agent_options(),
                mngr_ctx=test_ctx,
            )

    # The freshly-created host must have been destroyed so we never leak it.
    assert provider.destroyed_host_ids == [provider.get_host(HostName(LOCAL_HOST_NAME)).id]


def test_create_new_host_retained_on_failure_when_debug_flag_set(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the debug retain flag set, a failed new-host create keeps the host for inspection."""
    monkeypatch.setenv(_RETAIN_FLAG, "1")
    test_ctx = make_ctx_with_plugins(temp_mngr_ctx, [_RaiseInOnHostCreated()])
    provider = _make_recording_provider(local_provider)

    source_location = HostLocation(host=local_provider.create_host(HostName(LOCAL_HOST_NAME)), path=temp_work_dir)
    target_host = NewHostOptions(provider=ProviderInstanceName(LOCAL_PROVIDER_NAME), name=HostName(LOCAL_HOST_NAME))

    with _injected_provider(ProviderInstanceName(LOCAL_PROVIDER_NAME), test_ctx, provider):
        with pytest.raises(RuntimeError, match="simulated failure after host create"):
            create(
                source_location=source_location,
                target_host=target_host,
                agent_options=_make_create_agent_options(),
                mngr_ctx=test_ctx,
            )

    assert provider.destroyed_host_ids == []


def test_validate_session_adoption_skips_when_no_adopt_session(temp_mngr_ctx: MngrContext) -> None:
    # A no-op when --adopt was not passed: no agent-type resolution, no error.
    _validate_session_adoption(CreateAgentOptions(agent_type=AgentTypeName("claude")), temp_mngr_ctx)


def test_validate_session_adoption_passes_for_adoption_capable_agent(temp_mngr_ctx: MngrContext) -> None:
    # claude supports adoption; the agnostic gate passes (claude's own on_before_create
    # still fail-fasts on a bad session id).
    _validate_session_adoption(
        CreateAgentOptions(agent_type=AgentTypeName("claude"), adopt_session=("some-id",)),
        temp_mngr_ctx,
    )


def test_validate_session_adoption_rejects_agent_without_adoption_support(temp_mngr_ctx: MngrContext) -> None:
    with pytest.raises(UserInputError, match="supports session adoption"):
        _validate_session_adoption(
            CreateAgentOptions(agent_type=AgentTypeName("command"), adopt_session=("some-id",)),
            temp_mngr_ctx,
        )


def test_validate_session_adoption_allows_adopt_with_clone_source(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    # --adopt may be combined with --from: every named session plus the clone is made available
    # and the clone is the one resumed (handled by adopt_sessions), so the gate must not reject it.
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    _validate_session_adoption(
        CreateAgentOptions(
            agent_type=AgentTypeName("claude"),
            adopt_session=("some-id",),
            source_agent_state_location=HostLocation(host=host, path=tmp_path / "src"),
        ),
        temp_mngr_ctx,
    )
