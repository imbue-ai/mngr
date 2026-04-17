from pathlib import Path

from imbue.mngr.primitives import HostName
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import _normalize_start_args
from imbue.mngr_lima.limactl import _strip_ssh_config_quotes
from imbue.mngr_lima.limactl import host_name_from_instance_name
from imbue.mngr_lima.limactl import lima_instance_name


def test_lima_instance_name() -> None:
    name = lima_instance_name(HostName("my-host"), "mngr-")
    assert name == "mngr-my-host"


def test_lima_instance_name_custom_prefix() -> None:
    name = lima_instance_name(HostName("test"), "custom-")
    assert name == "custom-test"


def test_host_name_from_instance_name() -> None:
    result = host_name_from_instance_name("mngr-my-host", "mngr-")
    assert result == HostName("my-host")


def test_host_name_from_instance_name_no_match() -> None:
    result = host_name_from_instance_name("other-host", "mngr-")
    assert result is None


def test_host_name_from_instance_name_empty_suffix() -> None:
    result = host_name_from_instance_name("mngr-", "mngr-")
    assert result is None


def test_strip_ssh_config_quotes() -> None:
    assert _strip_ssh_config_quotes('"/home/josh/.lima/_config/user"') == "/home/josh/.lima/_config/user"
    assert _strip_ssh_config_quotes("127.0.0.1") == "127.0.0.1"
    assert _strip_ssh_config_quotes('"127.0.0.1"') == "127.0.0.1"
    assert _strip_ssh_config_quotes('"/path/with spaces/key"') == "/path/with spaces/key"
    assert _strip_ssh_config_quotes("  60022  ") == "60022"


def test_lima_ssh_config() -> None:
    config = LimaSshConfig(
        hostname="127.0.0.1",
        port=60022,
        user="josh",
        identity_file=Path("/home/josh/.lima/_config/user"),
    )
    assert config.hostname == "127.0.0.1"
    assert config.port == 60022
    assert config.user == "josh"
    assert config.identity_file == Path("/home/josh/.lima/_config/user")


def test_normalize_start_args_strips_gib_suffix() -> None:
    result = _normalize_start_args(("--cpus=2", "--memory=4GiB", "--disk=20GiB"))
    assert result == ["--cpus=2", "--memory=4", "--disk=20"]


def test_normalize_start_args_strips_mib_suffix() -> None:
    result = _normalize_start_args(("--memory=512MiB",))
    assert result == ["--memory=512"]


def test_normalize_start_args_strips_tib_suffix() -> None:
    result = _normalize_start_args(("--disk=1TiB",))
    assert result == ["--disk=1"]


def test_normalize_start_args_strips_lowercase_gb() -> None:
    result = _normalize_start_args(("--memory=4gb", "--disk=100GB"))
    assert result == ["--memory=4", "--disk=100"]


def test_normalize_start_args_preserves_plain_numbers() -> None:
    result = _normalize_start_args(("--memory=4", "--disk=20"))
    assert result == ["--memory=4", "--disk=20"]


def test_normalize_start_args_preserves_unrelated_flags() -> None:
    result = _normalize_start_args(("--cpus=2", "--vm-type=vz"))
    assert result == ["--cpus=2", "--vm-type=vz"]
