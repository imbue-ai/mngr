from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr import hookimpl
from imbue.mngr.api.create import _generate_unique_host_name
from imbue.mngr.api.create import _run_post_host_create_commands
from imbue.mngr.api.create import _write_host_env_vars
from imbue.mngr.api.create import create
from imbue.mngr.api.create import destroy_new_host_on_create_failure
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.api.testing import inject_provider_instance
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNameConflictError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import make_recording_destroy_provider
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE
from imbue.mngr.utils.testing import make_ctx_with_plugins


class _ScriptedProvider(LocalProviderInstance):
    """A LocalProviderInstance whose name-generation and create behavior are scripted.

    This exists so the host-name-uniqueness and conflict-retry logic in
    ``create.py`` can be exercised with deterministic, controlled inputs rather
    than relying on the local provider's fixed "localhost" semantics. It returns
    real local ``Host`` objects from ``create_host`` (so the result is a genuine
    ``OnlineHostInterface``), but lets the test dictate the candidate names, the
    set of "already existing" hosts, and how many times ``create_host`` raises
    ``HostNameConflictError`` before succeeding.
    """

    # Names that ``get_host_name`` hands out, one per call, in order.
    scripted_host_names: list[HostName] = Field(default_factory=list)
    # Names reported by ``discover_hosts`` as already taken.
    existing_host_names: list[HostName] = Field(default_factory=list)
    # Number of leading ``create_host`` calls that should raise a conflict.
    conflicts_before_success: int = Field(default=0)
    # Records every name passed to ``create_host`` (in call order).
    create_host_calls: list[HostName] = Field(default_factory=list)
    _get_host_name_index: int = 0

    def get_host_name(self, style: HostNameStyle) -> HostName:
        name = self.scripted_host_names[self._get_host_name_index]
        self._get_host_name_index += 1
        return name

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        return [
            DiscoveredHost(
                host_id=HostId.generate(),
                host_name=name,
                provider_name=self.name,
                host_state=None,
            )
            for name in self.existing_host_names
        ]

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        self.create_host_calls.append(name)
        if len(self.create_host_calls) <= self.conflicts_before_success:
            raise HostNameConflictError(self.name, name)
        # Return a genuine local Host. The local provider only knows how to make
        # the "localhost" host, and the scripted name is irrelevant to what we
        # assert, so delegate with LOCAL_HOST_NAME to get a real OnlineHostInterface.
        return super().create_host(HostName(LOCAL_HOST_NAME))


def _make_scripted_provider(
    local_provider: LocalProviderInstance,
    scripted_host_names: list[HostName] | None = None,
    existing_host_names: list[HostName] | None = None,
    conflicts_before_success: int = 0,
) -> _ScriptedProvider:
    return _ScriptedProvider(
        name=local_provider.name,
        host_dir=local_provider.host_dir,
        mngr_ctx=local_provider.mngr_ctx,
        scripted_host_names=scripted_host_names if scripted_host_names is not None else [],
        existing_host_names=existing_host_names if existing_host_names is not None else [],
        conflicts_before_success=conflicts_before_success,
    )


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


def test_generate_unique_host_name_skips_names_already_taken(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """_generate_unique_host_name skips candidates that collide with existing hosts.

    The scripted provider reports two existing hosts and hands out candidate
    names such that the first two collide with those existing names and the
    third is free. The function must skip the colliding candidates and return
    the first free one.
    """
    taken_one = HostName(f"taken-{uuid4().hex}")
    taken_two = HostName(f"taken-{uuid4().hex}")
    free_name = HostName(f"free-{uuid4().hex}")
    provider = _make_scripted_provider(
        local_provider,
        existing_host_names=[taken_one, taken_two],
        scripted_host_names=[taken_one, taken_two, free_name],
    )
    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=None,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    result = _generate_unique_host_name(provider, target, temp_mngr_ctx)

    assert result == free_name
    assert result not in {taken_one, taken_two}


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


@pytest.mark.allow_warnings(match="conflicted, regenerating")
def test_resolve_target_host_retries_auto_named_host_on_name_conflict(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """resolve_target_host regenerates the name and retries when create_host conflicts.

    Exercises the real retry loop in resolve_target_host: with an auto-named
    NewHostOptions (name=None), the first create_host raises
    HostNameConflictError, so the loop must regenerate a fresh name and call
    create_host again, ultimately returning a host. We assert create_host was
    invoked exactly twice and that the second call used a freshly generated name.
    """
    first_name = HostName(f"first-{uuid4().hex}")
    second_name = HostName(f"second-{uuid4().hex}")
    provider = _make_scripted_provider(
        local_provider,
        scripted_host_names=[first_name, second_name],
        conflicts_before_success=1,
    )
    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=None,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    with inject_provider_instance(provider, temp_mngr_ctx):
        result = resolve_target_host(target, temp_mngr_ctx)

    assert isinstance(result, OnlineHostInterface)
    # One conflicting call plus one successful call, each with a distinct name.
    assert provider.create_host_calls == [first_name, second_name]


def test_resolve_target_host_does_not_retry_user_specified_name_on_conflict(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """resolve_target_host re-raises a conflict for a user-specified name without retrying.

    When the user supplies an explicit host name (name is not None),
    max_attempts is 1, so a HostNameConflictError from create_host must
    propagate immediately rather than triggering name regeneration. We assert
    create_host was called exactly once and the error surfaced.
    """
    user_name = HostName(f"user-chosen-{uuid4().hex}")
    provider = _make_scripted_provider(
        local_provider,
        # get_host_name must not be consulted for a user-specified name; leave it
        # empty so any unexpected call would fail loudly.
        scripted_host_names=[],
        conflicts_before_success=99,
    )
    target = NewHostOptions(
        provider=ProviderInstanceName("local"),
        name=user_name,
        name_style=HostNameStyle.COOLNAME,
        tags={},
    )

    with inject_provider_instance(provider, temp_mngr_ctx):
        with pytest.raises(HostNameConflictError):
            resolve_target_host(target, temp_mngr_ctx)

    assert provider.create_host_calls == [user_name]


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


def test_destroy_new_host_on_create_failure_destroys_failed_new_host(
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """A failure inside the guard tears down the newly-created host and re-raises."""
    provider = make_recording_destroy_provider(local_provider)
    with pytest.raises(ValueError):
        with destroy_new_host_on_create_failure(local_host, provider):
            raise ValueError("provisioning blew up")
    assert provider.destroyed_host_ids == [local_host.id]


def test_destroy_new_host_on_create_failure_is_noop_on_success(
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """A clean exit must not destroy the host."""
    provider = make_recording_destroy_provider(local_provider)
    with destroy_new_host_on_create_failure(local_host, provider):
        pass
    assert provider.destroyed_host_ids == []


def test_destroy_new_host_on_create_failure_with_none_provider_only_reraises(local_host: Host) -> None:
    """An existing host (provider=None) is never torn down; the guard only re-raises.

    For an existing host the caller already owned, ``provider`` is ``None``, so
    the guard has no provider to destroy through -- it must simply let the
    original failure propagate. The no-destroy behavior here is structural (there
    is no provider to call ``destroy_host`` on), so the only observable effect is
    that the *exact* original exception propagates unchanged. The companion test
    ``..._destroys_failed_new_host`` covers the observable destroy path for the
    new-host case where a provider *is* present.
    """
    original_error = ValueError(f"boom-{uuid4().hex}")
    with pytest.raises(ValueError) as exc_info:
        with destroy_new_host_on_create_failure(local_host, None):
            raise original_error
    # The guard must re-raise the original exception untouched (not wrap or swallow it).
    assert exc_info.value is original_error


def test_destroy_new_host_on_create_failure_retains_host_when_debug_flag_set(
    local_provider: LocalProviderInstance,
    local_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The debug retain flag suppresses the teardown so a failed host can be inspected."""
    provider = make_recording_destroy_provider(local_provider)
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
    provider = make_recording_destroy_provider(local_provider)

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
    provider = make_recording_destroy_provider(local_provider)

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
