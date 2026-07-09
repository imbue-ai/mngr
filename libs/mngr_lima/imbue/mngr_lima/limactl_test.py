from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import _strip_ssh_config_quotes
from imbue.mngr_lima.limactl import host_name_from_instance_name
from imbue.mngr_lima.limactl import lima_instance_name
from imbue.mngr_lima.limactl import lima_instance_name_from_host_id
from imbue.mngr_lima.limactl import limactl_list
from imbue.mngr_lima.testing import install_fake_limactl


def test_lima_instance_name_from_host_id() -> None:
    host_id = HostId.generate()
    name = lima_instance_name_from_host_id(host_id, "mngr-")
    assert name == f"mngr-{host_id}"


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


def test_limactl_list_raises_lima_command_error_on_nonzero_exit(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashing ``limactl list`` surfaces as LimaCommandError, not a raw ProcessError.

    Regression guard: limactl_list must run with ``is_checked_after=False`` so the
    non-zero exit becomes the domain LimaCommandError its callers catch. Without it,
    ``run_process_to_completion`` raises a ConcurrencyGroup ProcessError first (and the
    ``raise LimaCommandError`` below is dead code), which slips past every caller that
    only catches LimaCommandError -- exactly how a limactl startup crash leaked out of
    discovery as an unclassified error.
    """
    bin_dir = tmp_path / "bin"
    install_fake_limactl(bin_dir, 'echo "panic: user: unknown userid 501" >&2\nexit 2\n', monkeypatch)

    with pytest.raises(LimaCommandError):
        limactl_list(temp_mngr_ctx.concurrency_group)


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
