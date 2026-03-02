from pathlib import Path

from imbue.mng.api.create import _write_host_env_vars
from imbue.mng.config.data_types import EnvVar
from imbue.mng.interfaces.host import HostEnvironmentOptions
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance


def test_write_host_env_vars_writes_explicit_env_vars(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
) -> None:
    """Test that _write_host_env_vars writes explicit env vars to the host env file."""
    host = local_provider.create_host(HostName("localhost"))

    environment = HostEnvironmentOptions(
        env_vars=(
            EnvVar(key="FOO", value="bar"),
            EnvVar(key="BAZ", value="qux"),
        ),
    )

    _write_host_env_vars(host, environment)

    host_env = host.get_env_vars()
    assert host_env["FOO"] == "bar"
    assert host_env["BAZ"] == "qux"


def test_write_host_env_vars_reads_env_files(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that _write_host_env_vars reads env files and writes to the host env file."""
    host = local_provider.create_host(HostName("localhost"))

    env_file = tmp_path / "test.env"
    env_file.write_text("FILE_VAR=from_file\nANOTHER=value\n")

    environment = HostEnvironmentOptions(
        env_files=(env_file,),
    )

    _write_host_env_vars(host, environment)

    host_env = host.get_env_vars()
    assert host_env["FILE_VAR"] == "from_file"
    assert host_env["ANOTHER"] == "value"


def test_write_host_env_vars_explicit_overrides_file(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that explicit env vars override values from env files."""
    host = local_provider.create_host(HostName("localhost"))

    env_file = tmp_path / "test.env"
    env_file.write_text("SHARED=from_file\nFILE_ONLY=present\n")

    environment = HostEnvironmentOptions(
        env_vars=(EnvVar(key="SHARED", value="from_explicit"),),
        env_files=(env_file,),
    )

    _write_host_env_vars(host, environment)

    host_env = host.get_env_vars()
    assert host_env["SHARED"] == "from_explicit"
    assert host_env["FILE_ONLY"] == "present"


def test_write_host_env_vars_skips_when_empty(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
) -> None:
    """Test that _write_host_env_vars does nothing when no env vars or files are specified."""
    host = local_provider.create_host(HostName("localhost"))

    environment = HostEnvironmentOptions()

    _write_host_env_vars(host, environment)

    # The host env file should not exist (no env vars written)
    host_env = host.get_env_vars()
    assert host_env == {}
