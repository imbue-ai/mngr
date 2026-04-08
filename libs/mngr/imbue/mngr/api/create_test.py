from pathlib import Path

import pytest

from imbue.mngr.api.create import _create_new_host
from imbue.mngr.api.create import _generate_unique_host_name
from imbue.mngr.api.create import _write_host_env_vars
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNameConflictError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance


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
            raise HostNameConflictError(name)
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
