"""Unit tests for OuterHost and the outer-host accessors."""

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
from imbue.mngr.hosts.outer_host import create_local_pyinfra_host
from imbue.mngr.hosts.outer_host import create_ssh_pyinfra_host_using_user_config
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


def test_prepend_env_exports_none_or_empty_is_unchanged() -> None:
    """No env vars -> the command is returned untouched."""
    assert _prepend_env_exports("docker build .", None) == "docker build ."
    assert _prepend_env_exports("docker build .", {}) == "docker build ."


def test_prepend_env_exports_uses_export_so_var_survives_compound_command() -> None:
    """Env vars must be ``export``ed, not bare ``KEY=VAL`` prefixed.

    Regression: a bare ``KEY=VAL command`` prefix only applies to the single
    simple command it precedes, so for a compound command like
    ``install && depot build`` the var is gone by the time ``depot build`` runs.
    Using ``export KEY=VAL &&`` sets it in the shell environment for the whole
    chain. This is the bug that made remote ``depot build`` fail with
    "missing API token" even though DEPOT_TOKEN was passed via env.
    """
    compound = "test -x /root/.depot/bin/depot || curl x | sh && /root/.depot/bin/depot build"
    result = _prepend_env_exports(compound, {"DEPOT_TOKEN": "depot_secret"})
    # The export must come first and chain into the whole command with &&, so
    # the var is in scope for the trailing ``depot build`` after the ``&&``/``||``.
    # (shlex.quote leaves the safe KEY=VAL unquoted.)
    assert result == "export DEPOT_TOKEN=depot_secret && " + compound
    # Guard against regressing to the broken bare-assignment prefix.
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
