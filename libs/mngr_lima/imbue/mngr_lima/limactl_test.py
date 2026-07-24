from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.errors import LimaInstanceNameTooLongError
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import _LIMA_SOCKET_PATH_OVERHEAD
from imbue.mngr_lima.limactl import _UNIX_PATH_MAX
from imbue.mngr_lima.limactl import _strip_ssh_config_quotes
from imbue.mngr_lima.limactl import host_name_from_instance_name
from imbue.mngr_lima.limactl import is_limactl_start_in_flight_for_instance
from imbue.mngr_lima.limactl import lima_instance_name
from imbue.mngr_lima.limactl import lima_instance_name_from_host_id
from imbue.mngr_lima.limactl import limactl_list
from imbue.mngr_lima.limactl import limactl_shell
from imbue.mngr_lima.limactl import resolve_lima_home
from imbue.mngr_lima.testing import install_fake_limactl


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


def test_resolve_lima_home_uses_env_var_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIMA_HOME", "/custom/lima/home")
    assert resolve_lima_home() == Path("/custom/lima/home")


def test_resolve_lima_home_defaults_to_dot_lima(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIMA_HOME", raising=False)
    assert resolve_lima_home() == Path.home() / ".lima"


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

    Regression guard: every limactl invocation funnels through ``_run_limactl``, which
    translates the ConcurrencyGroup ProcessError raised on a non-zero exit into the
    domain LimaCommandError its callers catch. Without that translation the raw
    ProcessError would slip past every caller that only catches LimaCommandError --
    exactly how a limactl startup crash leaked out of discovery as an unclassified error.
    """
    bin_dir = tmp_path / "bin"
    install_fake_limactl(bin_dir, 'echo "panic: user: unknown userid 501" >&2\nexit 2\n', monkeypatch)

    with pytest.raises(LimaCommandError):
        limactl_list(temp_mngr_ctx.concurrency_group)


def test_limactl_shell_returns_stdout_on_success(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limactl_shell returns the command's stdout when the invocation succeeds."""
    bin_dir = tmp_path / "bin"
    install_fake_limactl(bin_dir, 'echo "cloud-init done"\nexit 0\n', monkeypatch)

    assert limactl_shell(temp_mngr_ctx.concurrency_group, "some-instance", "true").strip() == "cloud-init done"


def test_limactl_shell_raises_lima_command_error_when_limactl_fails(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limactl that cannot reach the instance surfaces as LimaCommandError rather than
    a silently-returned non-zero exit code the caller may ignore.

    This is the consistency fix for the one helper that previously never raised: a
    limactl startup crash mid-command is now reported like every other limactl failure.
    """
    bin_dir = tmp_path / "bin"
    install_fake_limactl(bin_dir, 'echo "panic: user: unknown userid 501" >&2\nexit 2\n', monkeypatch)

    with pytest.raises(LimaCommandError):
        limactl_shell(temp_mngr_ctx.concurrency_group, "some-instance", "true")


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


_INSTANCE = "mngr-host-0123456789abcdef"


def test_start_in_flight_matches_existing_instance_shape() -> None:
    # limactl_start_existing: `limactl --log-level=info start <name>`
    argvs = [("limactl", "--log-level=info", "start", _INSTANCE)]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is True


def test_start_in_flight_matches_new_instance_shape() -> None:
    # limactl_start_new: `limactl --log-level=info start --name=<name> <yaml> [start_args]`
    argvs = [("limactl", "--log-level=info", "start", f"--name={_INSTANCE}", "/tmp/x.yaml", "--cpus=8")]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is True


def test_start_in_flight_matches_bare_invocation_and_absolute_program_path() -> None:
    argvs = [("/opt/homebrew/bin/limactl", "start", _INSTANCE)]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is True


def test_start_in_flight_requires_exact_instance_name_not_prefix() -> None:
    # A start for `mngr-host-0123456789abcdef2` must not be read as booting `_INSTANCE`.
    argvs = [
        ("limactl", "--log-level=info", "start", _INSTANCE + "2"),
        ("limactl", "start", f"--name={_INSTANCE}2", "/tmp/x.yaml"),
    ]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is False


def test_start_in_flight_ignores_other_limactl_subcommands() -> None:
    argvs = [
        ("limactl", "stop", _INSTANCE),
        ("limactl", "list", "--json"),
        ("limactl", "--log-level=info", "delete", "--force", _INSTANCE),
    ]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is False


def test_start_in_flight_ignores_non_limactl_process_mentioning_instance() -> None:
    # A grep/tail whose args merely contain the instance name is not a `limactl start`.
    argvs = [("grep", "start", _INSTANCE), ("tail", "-f", f"/var/log/{_INSTANCE}.log")]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is False


def test_start_in_flight_false_for_empty_and_short_argvs() -> None:
    argvs = [(), ("limactl",), ("limactl", "start")]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is False


def test_start_in_flight_true_when_any_process_in_list_matches() -> None:
    argvs = [
        ("limactl", "list", "--json"),
        ("some-daemon", "--serve"),
        ("limactl", "--log-level=info", "start", _INSTANCE),
    ]
    assert is_limactl_start_in_flight_for_instance(_INSTANCE, argvs) is True
