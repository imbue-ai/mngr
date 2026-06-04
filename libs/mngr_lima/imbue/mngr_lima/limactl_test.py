from pathlib import Path

from imbue.mngr.primitives import HostName
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import _parse_show_ssh_output
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


def test_parse_show_ssh_output_extracts_all_fields() -> None:
    # Shape of `limactl show-ssh --format config <instance>`: an indented,
    # quote-wrapped SSH-config block. The parser must strip quotes, coerce the
    # port to int, and map each directive to the right field.
    stdout = (
        "Host lima-mngr-my-host\n"
        '  HostName "127.0.0.1"\n'
        "  Port 60022\n"
        '  User "josh"\n'
        '  IdentityFile "/home/josh/.lima/_config/user"\n'
    )

    config = _parse_show_ssh_output(stdout)

    assert config == LimaSshConfig(
        hostname="127.0.0.1",
        port=60022,
        user="josh",
        identity_file=Path("/home/josh/.lima/_config/user"),
    )


def test_parse_show_ssh_output_falls_back_to_defaults_for_missing_directives() -> None:
    # Only Port is present; the other fields must take Lima's documented
    # defaults rather than being left unset or carrying a stale value.
    config = _parse_show_ssh_output("Host lima-mngr-my-host\n  Port 50051\n")

    assert config.port == 50051
    assert config.hostname == "127.0.0.1"
    assert config.user == "root"
