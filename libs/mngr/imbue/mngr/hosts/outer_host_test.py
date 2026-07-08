"""Unit tests for OuterHost and the outer-host accessors."""

import stat
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from paramiko import SSHException
from pyinfra.api.exceptions import ConnectError
from pyinfra.api.host import Host as PyinfraHost

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.hosts.outer_host import _is_transient_ssh_error
from imbue.mngr.hosts.outer_host import _prepend_env_exports
from imbue.mngr.hosts.outer_host import _sftp_walk
from imbue.mngr.hosts.outer_host import create_local_pyinfra_host
from imbue.mngr.hosts.outer_host import create_ssh_pyinfra_host_using_user_config
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId


def test_outer_host_satisfies_outer_host_interface(temp_mngr_ctx: MngrContext) -> None:
    """A constructed OuterHost is an instance of OuterHostInterface."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(outer, OuterHostInterface)


def test_ensure_connected_wraps_paramiko_value_error(temp_mngr_ctx: MngrContext) -> None:
    """paramiko's bare ValueError on connect is surfaced as a structured HostConnectionError.

    A malformed or half-written ``.pub`` next to the private key makes paramiko's
    per-connection certificate probe raise ``ValueError: Not enough fields for
    public blob``. It must become a ``MngrError`` so best-effort callers (e.g.
    host discovery) treat it as a per-host connection failure rather than letting
    it abort the whole operation.
    """

    class _ConnectFailingHost:
        name = "fake-host"
        connector_cls = PyinfraHost
        connected = False

        def connect(self, raise_exceptions: bool = False) -> None:
            raise ValueError("Not enough fields for public blob")

    connector = PyinfraConnector(cast(PyinfraHost, _ConnectFailingHost()))
    outer = OuterHost(id=HostId.generate(), connector=connector, mngr_ctx=temp_mngr_ctx)

    with pytest.raises(HostConnectionError, match="Not enough fields for public blob"):
        outer._ensure_connected()


def test_prepend_env_exports_none_or_empty_is_unchanged() -> None:
    """No env vars -> the command is returned untouched."""
    assert _prepend_env_exports("docker build .", None) == "docker build ."
    assert _prepend_env_exports("docker build .", {}) == "docker build ."


def test_prepend_env_exports_uses_export_so_var_survives_compound_command() -> None:
    """Env vars must be ``export``ed, not bare ``KEY=VAL`` prefixed.

    A bare ``KEY=VAL command`` prefix only applies to the single simple command
    it precedes, so for a compound command like ``install && depot build`` the
    var would be gone by the time ``depot build`` runs. ``export KEY=VAL &&``
    sets it in the shell environment for the whole chain.
    """
    compound = "test -x /root/.depot/bin/depot || curl x | sh && /root/.depot/bin/depot build"
    result = _prepend_env_exports(compound, {"DEPOT_TOKEN": "depot_secret"})
    # The export must come first and chain into the whole command with &&, so
    # the var is in scope for the trailing ``depot build`` after the ``&&``/``||``.
    # (shlex.quote leaves the safe KEY=VAL unquoted.)
    assert result == "export DEPOT_TOKEN=depot_secret && " + compound
    # Must not use a bare ``KEY=VAL`` assignment prefix.
    assert not result.startswith("DEPOT_TOKEN=")


def test_prepend_env_exports_quotes_values_with_shell_metacharacters() -> None:
    """Values containing shell metacharacters are shlex-quoted so they can't break out."""
    result = _prepend_env_exports("run", {"TOK": "a b;rm -rf /"})
    assert result == "export 'TOK=a b;rm -rf /' && run"


def test_outer_host_local_is_local(temp_mngr_ctx: MngrContext) -> None:
    """An OuterHost wrapping a local pyinfra connector reports is_local=True."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    assert outer.is_local is True


def test_outer_host_local_get_ssh_connection_info_is_none(temp_mngr_ctx: MngrContext) -> None:
    """Local OuterHost has no SSH connection info."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    assert outer.get_ssh_connection_info() is None


def test_outer_host_local_executes_command(temp_mngr_ctx: MngrContext) -> None:
    """A local OuterHost can run a shell command and capture stdout."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    result = outer.execute_idempotent_command("echo hello-from-outer")
    assert result.success
    assert "hello-from-outer" in result.stdout


def test_outer_host_list_directory_local(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """list_directory on a local OuterHost reports entries with absolute paths and types."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "nested.txt").write_text("n")
    (root / "top.txt").write_text("t")

    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(create_local_pyinfra_host()),
        mngr_ctx=temp_mngr_ctx,
    )

    # Non-recursive: only the immediate children.
    shallow = {entry.path: entry.file_type for entry in outer.list_directory(root)}
    assert shallow == {
        str(root / "sub"): FileType.DIRECTORY,
        str(root / "top.txt"): FileType.FILE,
    }

    # Recursive: descends into subdirectories and reports the full tree with types.
    deep = {entry.path: entry.file_type for entry in outer.list_directory(root, recursive=True)}
    assert deep == {
        str(root / "sub"): FileType.DIRECTORY,
        str(root / "sub" / "nested.txt"): FileType.FILE,
        str(root / "top.txt"): FileType.FILE,
    }

    # A local host surfaces a mode string for each entry.
    perms_by_path = {entry.path: entry.permissions for entry in outer.list_directory(root)}
    top_perms = perms_by_path[str(root / "top.txt")]
    sub_perms = perms_by_path[str(root / "sub")]
    assert top_perms is not None and top_perms.startswith("-")
    assert sub_perms is not None and sub_perms.startswith("d")

    # A missing directory yields an empty list rather than raising.
    assert outer.list_directory(root / "does-not-exist") == []


def test_outer_host_list_directory_local_symlink_classified_as_symlink(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A symlink is classified as SYMLINK (lstat semantics) and not descended into.

    The classifier reports the link's own type rather than its target's, so a
    symlink to a directory is SYMLINK -- matching the remote SFTP path, which
    also reads symlink attributes rather than following them.
    """
    root = tmp_path / "tree"
    (root / "real_dir").mkdir(parents=True)
    (root / "link").symlink_to(root / "real_dir")

    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(create_local_pyinfra_host()),
        mngr_ctx=temp_mngr_ctx,
    )

    entries = {entry.path: entry for entry in outer.list_directory(root)}
    assert entries[str(root / "real_dir")].file_type == FileType.DIRECTORY
    assert entries[str(root / "link")].file_type == FileType.SYMLINK
    # The symlink's mode string starts with 'l'.
    link_perms = entries[str(root / "link")].permissions
    assert link_perms is not None and link_perms.startswith("l")

    # Recursing does not follow the symlink (no entries appear under it).
    deep_paths = {entry.path for entry in outer.list_directory(root, recursive=True)}
    assert not any(p.startswith(str(root / "link") + "/") for p in deep_paths)


class _FakeSftpAttr:
    """Minimal stand-in for a paramiko SFTPAttributes entry."""

    def __init__(self, filename: str, st_mode: int | None, st_mtime: int = 0, st_size: int = 0) -> None:
        self.filename = filename
        self.st_mode = st_mode
        self.st_mtime = st_mtime
        self.st_size = st_size


class _FakeSftp:
    """A fake SFTP client whose ``listdir_attr`` serves a fixed directory tree.

    Lets ``_sftp_walk`` be tested without a network: a directory not present in
    the map raises ``IOError`` (as paramiko does for a missing dir).
    """

    def __init__(self, entries_by_dir: dict[str, list[_FakeSftpAttr]]) -> None:
        self._entries_by_dir = entries_by_dir

    def listdir_attr(self, path: str) -> list[_FakeSftpAttr]:
        if path not in self._entries_by_dir:
            raise IOError(f"No such directory: {path}")
        return self._entries_by_dir[path]


def test_sftp_walk_classifies_types_permissions_and_recurses() -> None:
    """_sftp_walk classifies the full type set from st_mode, fills permissions, and
    recurses into directories but not symlinks -- matching the local listing."""
    sftp = _FakeSftp(
        {
            "/base": [
                _FakeSftpAttr("sub", stat.S_IFDIR | 0o755),
                _FakeSftpAttr("f.txt", stat.S_IFREG | 0o644, st_size=5),
                _FakeSftpAttr("link", stat.S_IFLNK | 0o777),
                _FakeSftpAttr("pipe", stat.S_IFIFO | 0o644),
            ],
            "/base/sub": [
                _FakeSftpAttr("nested.txt", stat.S_IFREG | 0o600, st_size=3),
            ],
            # Present but must never be listed: a symlink is not descended into.
            "/base/link": [_FakeSftpAttr("should_not_appear", stat.S_IFREG | 0o644)],
        }
    )

    entries = {e.path: e for e in _sftp_walk(cast(Any, sftp), "/base", recursive=True)}

    assert entries["/base/sub"].file_type == FileType.DIRECTORY
    assert entries["/base/f.txt"].file_type == FileType.FILE
    assert entries["/base/link"].file_type == FileType.SYMLINK
    assert entries["/base/pipe"].file_type == FileType.PIPE
    # Permissions are the stat.filemode string.
    assert entries["/base/f.txt"].permissions == "-rw-r--r--"
    assert entries["/base/sub"].permissions is not None
    assert entries["/base/sub"].permissions.startswith("d")
    assert entries["/base/link"].permissions is not None
    assert entries["/base/link"].permissions.startswith("l")
    # Recursion descended into the directory...
    assert entries["/base/sub/nested.txt"].file_type == FileType.FILE
    # ...but not into the symlink.
    assert not any(p.startswith("/base/link/") for p in entries)


def test_sftp_walk_missing_dir_returns_empty() -> None:
    """A directory that cannot be listed yields no entries rather than raising."""
    assert _sftp_walk(cast(Any, _FakeSftp({})), "/nope", recursive=True) == []


def test_sftp_walk_without_st_mode_falls_back_to_file() -> None:
    """When SFTP omits st_mode, the entry classifies as FILE with no permissions."""
    sftp = _FakeSftp({"/base": [_FakeSftpAttr("x", None)]})
    [entry] = _sftp_walk(cast(Any, sftp), "/base", recursive=False)
    assert entry.file_type == FileType.FILE
    assert entry.permissions is None


def test_host_is_outer_host_interface() -> None:
    """A regular Host is also an OuterHostInterface (so providers can return Host as outer)."""
    assert issubclass(Host, OuterHostInterface)


def test_outer_host_get_name_strips_at_prefix(temp_mngr_ctx: MngrContext) -> None:
    """OuterHost.get_name strips the leading '@' that pyinfra uses for local connectors."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    name = outer.get_name()
    assert not str(name).startswith("@")
    assert str(name) == "local"


def test_create_ssh_pyinfra_host_carries_user_and_port() -> None:
    """The SSH-pyinfra-host helper sets ssh_user and ssh_port on host data."""
    pyinfra_host = create_ssh_pyinfra_host_using_user_config(
        hostname="example.com",
        port=2222,
        user="alice",
    )
    assert pyinfra_host.data.get("ssh_user") == "alice"
    assert pyinfra_host.data.get("ssh_port") == 2222


def test_create_ssh_pyinfra_host_no_key_set() -> None:
    """The SSH-pyinfra-host helper does NOT set ssh_key (deferred to user's ~/.ssh)."""
    pyinfra_host = create_ssh_pyinfra_host_using_user_config(hostname="example.com")
    assert pyinfra_host.data.get("ssh_key") is None


def test_outer_host_streaming_local_calls_on_line_per_line(temp_mngr_ctx: MngrContext) -> None:
    """execute_streaming_command on a local OuterHost calls on_line for each output line."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    received: list[str] = []
    result = outer.execute_streaming_command(
        "printf 'one\\ntwo\\nthree\\n'",
        received.append,
    )
    assert result.success
    assert received == ["one", "two", "three"]
    # The full stdout should also be captured in the result.
    assert "one" in result.stdout
    assert "three" in result.stdout


def test_outer_host_streaming_local_captures_failure(temp_mngr_ctx: MngrContext) -> None:
    """execute_streaming_command surfaces non-zero exit codes via CommandResult.success."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    received: list[str] = []
    result = outer.execute_streaming_command(
        "echo before-fail; exit 7",
        received.append,
    )
    assert not result.success
    assert "before-fail" in received


def test_outer_host_streaming_local_streams_stderr(temp_mngr_ctx: MngrContext) -> None:
    """stderr lines also reach on_line and end up on the result.stderr field."""
    pyinfra_host = create_local_pyinfra_host()
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(pyinfra_host),
        mngr_ctx=temp_mngr_ctx,
    )
    received: list[str] = []
    result = outer.execute_streaming_command(
        "echo to-stdout; echo to-stderr 1>&2",
        received.append,
    )
    assert result.success
    assert "to-stdout" in received
    assert "to-stderr" in received
    assert "to-stdout" in result.stdout
    assert "to-stderr" in result.stderr


class _FakePyinfraHostRaisingOnConnect:
    """Minimal pyinfra-host stand-in whose ``connect()`` raises a configured ConnectError.

    Just enough surface for ``OuterHost._ensure_connected`` to exercise its
    ``ConnectError`` -> ``HostAuthenticationError`` / ``HostConnectionError``
    classifier without touching the network or paramiko.
    """

    def __init__(self, message: str) -> None:
        self.connected = False
        self.name = "fake-ssh-host"
        self.connector_cls = type("SSHConnector", (), {})
        self._message = message

    def connect(self, raise_exceptions: bool = False) -> None:
        raise ConnectError(self._message)


@pytest.mark.parametrize(
    "message",
    [
        # Exact wording produced by pyinfra's StrictPolicy when known_hosts has no
        # entry for the target. The lower() in _ensure_connected normalises the
        # capitalised "No host key" to "no host key".
        "SSH error: StrictPolicy: No host key for [example.com]:2222 found in known_hosts",
        # Wording produced by pyinfra's ssh connector when paramiko reports an
        # AuthenticationException; covers the pre-existing branch of the
        # discriminator alongside the new "no host key for" branch.
        "Authentication error (username=alice): bad password",
    ],
    ids=["missing-host-key", "auth-failure"],
)
def test_ensure_connected_classifies_trust_failures_as_auth_error(
    temp_mngr_ctx: MngrContext,
    message: str,
) -> None:
    """Trust failures (missing host key, bad credentials) raise HostAuthenticationError.

    Regression test for ``mngr gc`` crashing on hosts whose SSH host key is
    missing from ``known_hosts``: pyinfra wraps that as ``ConnectError("SSH
    error: StrictPolicy: No host key for ...")``, and ``_ensure_connected``
    must classify it as ``HostAuthenticationError`` so callers that only catch
    that subclass (e.g. ``_gc_single_host_work_dir``) skip the host with a
    warning instead of letting the bare ``HostConnectionError`` propagate.
    """
    fake = _FakePyinfraHostRaisingOnConnect(message)
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(cast(PyinfraHost, fake)),
        mngr_ctx=temp_mngr_ctx,
    )

    with pytest.raises(HostAuthenticationError):
        outer._ensure_connected()


def test_ensure_connected_classifies_unrelated_connect_errors_as_connection_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Non-trust ConnectErrors stay as the generic HostConnectionError, not auth."""
    fake = _FakePyinfraHostRaisingOnConnect(
        "Could not resolve hostname: example.invalid",
    )
    outer = OuterHost(
        id=HostId.generate(),
        connector=PyinfraConnector(cast(PyinfraHost, fake)),
        mngr_ctx=temp_mngr_ctx,
    )

    with pytest.raises(HostConnectionError) as excinfo:
        outer._ensure_connected()
    # HostAuthenticationError subclasses HostConnectionError, so we must check
    # the concrete type to confirm we did NOT promote a generic connectivity
    # failure to a trust failure.
    assert not isinstance(excinfo.value, HostAuthenticationError)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (OSError("Socket is closed"), True),
        (OSError("No such file or directory"), False),
        (SSHException("SSH session not active"), True),
        (EOFError(), True),
        (TimeoutError("Timed out reading output"), True),
        (ValueError("not transient"), False),
    ],
    ids=["socket-closed", "other-os-error", "ssh-exception", "eof-error", "timeout-error", "non-os-error"],
)
def test_is_transient_ssh_error_classifies_timeout_as_transient(exception: BaseException, expected: bool) -> None:
    """Regression: ``TimeoutError`` from pyinfra's ``read_output_buffers`` must be classified transient.

    pyinfra raises a bare ``TimeoutError`` (Python builtin) when an SSH
    command's response doesn't arrive within the per-command read
    timeout -- for example, when the remote sshd is reloaded mid-read
    during cloud-init. Without TimeoutError in the transient set, the
    retry loop didn't fire and the exception propagated all the way out
    of host creation. ``TimeoutError`` is an ``OSError`` subclass on
    Python 3, so the classifier's ordering matters: the TimeoutError
    branch must precede the narrow "Socket is closed" OSError check.
    """
    assert _is_transient_ssh_error(exception) is expected
