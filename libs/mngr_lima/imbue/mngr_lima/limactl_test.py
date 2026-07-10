from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr_lima.errors import LimaInstanceNameTooLongError
from imbue.mngr_lima.limactl import _LIMA_SOCKET_PATH_OVERHEAD
from imbue.mngr_lima.limactl import _UNIX_PATH_MAX
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import _strip_ssh_config_quotes
from imbue.mngr_lima.limactl import host_name_from_instance_name
from imbue.mngr_lima.limactl import lima_instance_name
from imbue.mngr_lima.limactl import lima_instance_name_from_host_id


def _lima_socket_path_length(instance_name: str, lima_home: Path) -> int:
    """Length of the SSH socket path Lima derives from an instance name and LIMA_HOME."""
    return len(str(lima_home)) + len(instance_name) + _LIMA_SOCKET_PATH_OVERHEAD


def test_lima_instance_name_from_host_id_keeps_full_id_when_it_fits() -> None:
    # A short home path leaves ample room, so the name reproduces the original
    # ``<prefix><host_id>`` scheme verbatim (full 32-char hex, no truncation).
    host_id = HostId.generate()
    name = lima_instance_name_from_host_id(host_id, "mngr-", lima_home=Path("/home/x/.lima"))
    assert name == f"mngr-{host_id}"


def test_lima_instance_name_from_host_id_truncates_to_fit_long_home() -> None:
    # Reproduces the reported overflow: a 13-char username plus the
    # ``minds-staging-`` prefix pushes the untruncated 51-char name one char
    # over Lima's ceiling. The tail must be shortened so the socket path fits.
    host_id = HostId.generate()
    lima_home = Path("/Users/gabeguralnick/.lima")
    name = lima_instance_name_from_host_id(host_id, "minds-staging-", lima_home=lima_home)
    assert name.startswith("minds-staging-host-")
    assert _lima_socket_path_length(name, lima_home) < _UNIX_PATH_MAX
    # The tail is drawn from (a prefix of) the host id's hex, not something else.
    assert host_id.get_uuid().hex.startswith(name.removeprefix("minds-staging-host-"))


def test_lima_instance_name_from_host_id_is_deterministic() -> None:
    host_id = HostId.generate()
    lima_home = Path("/Users/gabeguralnick/.lima")
    first = lima_instance_name_from_host_id(host_id, "minds-staging-", lima_home=lima_home)
    second = lima_instance_name_from_host_id(host_id, "minds-staging-", lima_home=lima_home)
    assert first == second


def test_lima_instance_name_from_host_id_raises_when_no_id_fits() -> None:
    # An absurdly long LIMA_HOME leaves no room for even a minimal hex tail.
    host_id = HostId.generate()
    with pytest.raises(LimaInstanceNameTooLongError):
        lima_instance_name_from_host_id(host_id, "minds-staging-", lima_home=Path("/" + "d" * 90 + "/.lima"))


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
