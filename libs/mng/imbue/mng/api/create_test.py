from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.create import _generate_unique_host_name
from imbue.mng.api.create import _write_host_env_vars
from imbue.mng.api.create import resolve_target_host
from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.host import HostEnvironmentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.mock_provider_test import MockProviderInstance


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
    temp_mng_ctx: MngContext,
    temp_host_dir: Path,
) -> None:
    """resolve_target_host should return the host directly when given an existing OnlineHostInterface."""
    assert isinstance(local_host, OnlineHostInterface)

    resolved = resolve_target_host(local_host, temp_mng_ctx)
    assert resolved.id == local_host.id


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
# _generate_unique_host_name Tests
# =============================================================================


class _SequentialNameProvider(MockProviderInstance):
    """Mock provider that returns names from a predefined sequence.

    Also overrides discover_hosts to return a configurable set of discovered hosts
    for uniqueness testing.
    """

    sequential_names: tuple[HostName, ...] = Field(default=(), description="Names to return in sequence")
    discovered_hosts_override: tuple[DiscoveredHost, ...] = Field(
        default=(), description="Hosts to return from discover_hosts"
    )
    _call_count: int = PrivateAttr(default=0)

    def get_host_name(self, style: HostNameStyle) -> HostName:
        index = min(self._call_count, len(self.sequential_names) - 1)
        self._call_count += 1
        return self.sequential_names[index]

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        if self.discovered_hosts_override:
            return list(self.discovered_hosts_override)
        return super().discover_hosts(cg=cg, include_destroyed=include_destroyed)


def _make_discovered_host(name: str, provider_name: str = "test") -> DiscoveredHost:
    return DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName(name),
        provider_name=ProviderInstanceName(provider_name),
    )


def test_generate_unique_host_name_no_existing_hosts(
    temp_mng_ctx: MngContext,
    temp_host_dir: Path,
) -> None:
    """_generate_unique_host_name should return the first name when no hosts exist."""
    provider = _SequentialNameProvider(
        sequential_names=(HostName("alpha"),),
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )

    result = _generate_unique_host_name(provider, HostNameStyle.ASTRONOMY, temp_mng_ctx)
    assert result == HostName("alpha")


def test_generate_unique_host_name_skips_colliding_names(
    temp_mng_ctx: MngContext,
    temp_host_dir: Path,
) -> None:
    """_generate_unique_host_name should skip names that collide with existing hosts."""
    provider = _SequentialNameProvider(
        sequential_names=(HostName("taken"), HostName("also-taken"), HostName("unique")),
        discovered_hosts_override=(
            _make_discovered_host("taken", "test"),
            _make_discovered_host("also-taken", "test"),
        ),
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )

    result = _generate_unique_host_name(provider, HostNameStyle.ASTRONOMY, temp_mng_ctx)
    assert result == HostName("unique")


def test_generate_unique_host_name_accepts_fixed_name_provider(
    temp_mng_ctx: MngContext,
    temp_host_dir: Path,
) -> None:
    """_generate_unique_host_name should accept a colliding name from a fixed-name provider.

    Providers like the local provider always return the same name (e.g. "localhost")
    even when a host with that name already exists. The function should detect this
    and return the name as-is.
    """
    provider = _SequentialNameProvider(
        sequential_names=(HostName("localhost"), HostName("localhost")),
        discovered_hosts_override=(_make_discovered_host("localhost", "test"),),
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )

    result = _generate_unique_host_name(provider, HostNameStyle.ASTRONOMY, temp_mng_ctx)
    assert result == HostName("localhost")


def test_generate_unique_host_name_raises_after_max_attempts(
    temp_mng_ctx: MngContext,
    temp_host_dir: Path,
) -> None:
    """_generate_unique_host_name should raise MngError when all random names collide."""
    # Each attempt returns a different name, but all collide with existing hosts.
    # This ensures the fixed-name-provider detection doesn't short-circuit.
    taken_names = tuple(HostName(f"taken-{i}") for i in range(20))
    provider = _SequentialNameProvider(
        sequential_names=taken_names,
        discovered_hosts_override=tuple(_make_discovered_host(str(n), "test") for n in taken_names),
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )

    with pytest.raises(MngError, match="Failed to generate a unique host name"):
        _generate_unique_host_name(provider, HostNameStyle.ASTRONOMY, temp_mng_ctx)
