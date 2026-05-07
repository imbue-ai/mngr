"""Concrete OuterHost: a minimal pyinfra-backed host with no agent / lifecycle /
snapshot / tag machinery.

Used to access the underlying machine (VPS, local box, SSH-reachable docker
daemon host) that hosts a container/sandbox managed by mngr. Has no host_dir,
no certified data, no agents, no idle tracking. Just file ops, command
execution, and SSH info.

A regular Host (which implements OnlineHostInterface, which extends
OuterHostInterface) is also an OuterHostInterface, so providers whose outer
is itself an mngr-managed Host can return that Host directly. OuterHost is for
the cases where the outer is *not* an mngr-managed host (e.g. the VPS hosting
a container, or the SSH-reachable docker daemon machine).
"""

from __future__ import annotations

import io
import os
import shlex
import threading
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import IO
from typing import Iterator
from typing import Mapping
from uuid import uuid4

from loguru import logger
from paramiko import ChannelException
from paramiko import SFTPClient
from paramiko import SSHException
from paramiko import Transport
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.command import StringCommand
from pyinfra.api.exceptions import ConnectError
from pyinfra.api.inventory import Inventory
from pyinfra.connectors.util import CommandOutput
from pyinfra.connectors.util import OutputLine
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_chain
from tenacity import wait_fixed

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import LOCAL_CONNECTOR_NAME
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostName


def create_local_pyinfra_host() -> PyinfraHost:
    """Create a pyinfra host that executes commands on the local machine.

    Mirrors ``LocalProviderInstance.create_local_pyinfra_host``. Pyinfra's
    LocalConnector is selected automatically when the host name starts with
    ``@``.
    """
    names_data = (["@local"], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)
    pyinfra_host = inventory.get_host("@local")
    pyinfra_host.init(state)
    return pyinfra_host


def create_ssh_pyinfra_host_using_user_config(
    hostname: str,
    port: int | None = None,
    user: str | None = None,
) -> PyinfraHost:
    """Create a pyinfra SSH host that defers credential resolution to OpenSSH.

    Used for outer-host SSH connections where mngr does not own the credentials
    (e.g. ``DOCKER_HOST=ssh://user@host``). The user's ``~/.ssh/config`` and
    ssh-agent supply the key.

    No ``ssh_key`` / ``ssh_known_hosts_file`` is set so paramiko falls back to
    its default lookup chain (``~/.ssh/id_*``, agent, ``~/.ssh/known_hosts``).
    """
    host_data: dict[str, object] = {}
    if user is not None:
        host_data["ssh_user"] = user
    if port is not None:
        host_data["ssh_port"] = port

    names_data = ([(hostname, host_data)], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)
    pyinfra_host = inventory.get_host(hostname)
    pyinfra_host.init(state)
    return pyinfra_host


def _is_transient_ssh_error(exception: BaseException) -> bool:
    """Check if the exception is a transient SSH connection error worth retrying."""
    if isinstance(exception, OSError) and "Socket is closed" in str(exception):
        return True
    if isinstance(exception, SSHException):
        return True
    if isinstance(exception, EOFError):
        return True
    return False


_retry_on_transient_ssh_error = retry(
    retry=retry_if_exception(_is_transient_ssh_error),
    stop=stop_after_attempt(5),
    wait=wait_chain(
        wait_fixed(0),
        wait_fixed(1),
        wait_fixed(3),
        wait_fixed(6),
    ),
    reraise=True,
)


def _get_ssh_transport(pyinfra_host: Any) -> Transport | None:
    """Extract the paramiko Transport from a pyinfra host, or None for non-SSH connectors."""
    try:
        client = pyinfra_host.connector.client
    except AttributeError:
        return None
    if client is not None:
        return client.get_transport()
    return None


class _StreamingOutputAccumulator(MutableModel):
    """Adapter that fits into ``ConcurrencyGroup.run_process_to_completion``'s
    ``on_output(line, is_stdout)`` shape while exposing the merged-line
    ``on_line(stripped_line)`` shape used by ``OuterHost.execute_streaming_command``.

    Also accumulates stdout / stderr text so the caller can build a final
    ``CommandResult``. Lines arrive with their trailing newline; we strip it
    before forwarding so callers see clean lines (matching the SSH path).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    on_line: Callable[[str], None] = Field(description="Callback invoked with each clean line")
    stdout_lines: list[str] = Field(default_factory=list, description="Captured stdout lines")
    stderr_lines: list[str] = Field(default_factory=list, description="Captured stderr lines")

    def __call__(self, line: str, is_stdout: bool) -> None:
        stripped = line.rstrip("\n")
        self.on_line(stripped)
        if is_stdout:
            self.stdout_lines.append(stripped)
        else:
            self.stderr_lines.append(stripped)

    @property
    def stdout(self) -> str:
        return "\n".join(self.stdout_lines) + ("\n" if self.stdout_lines else "")

    @property
    def stderr(self) -> str:
        return "\n".join(self.stderr_lines) + ("\n" if self.stderr_lines else "")


class _SSHStderrState(MutableModel):
    """State for the daemon thread that streams stderr from a paramiko channel.

    The thread reads lines off ``stderr`` until EOF, calls ``on_line`` for each
    one, and accumulates the raw line list. Errors during reading are swallowed
    (logged at debug); the stdout reader on the main thread is the source of
    truth for command failure.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stderr: Any = Field(repr=False, description="Paramiko channel stderr file-like object")
    on_line: Callable[[str], None] = Field(description="Callback invoked with each clean line")
    lines: list[str] = Field(default_factory=list, description="Captured stderr lines")


def _drain_ssh_stderr_into(state: _SSHStderrState) -> None:
    """Daemon-thread target that reads ``state.stderr`` line-by-line into ``state.lines``."""
    try:
        for raw in iter(state.stderr.readline, ""):
            stripped = raw.rstrip("\n")
            state.lines.append(stripped)
            state.on_line(stripped)
    except (OSError, SSHException, EOFError) as exc:
        logger.debug("stderr reader stopped: {}", exc)


class OuterHost(OuterHostInterface):
    """A minimal, agent-less host backed by a pyinfra connector.

    Implements only the safe primitives of OuterHostInterface. Construction
    is a pure function of (connector, mngr_ctx, id) — no provider, no host_dir,
    no agents.
    """

    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="The mngr context")

    # Set to True by disconnect() to suppress paramiko cleanup in __del__.
    _explicitly_disconnected: bool = PrivateAttr(default=False)

    @property
    def is_local(self) -> bool:
        """Check if this host uses the local connector."""
        return self.connector.connector_cls_name == LOCAL_CONNECTOR_NAME

    def get_name(self) -> HostName:
        """Return the human-readable name of this host."""
        name = self.connector.name
        if name.startswith("@"):
            name = name[1:]
        return HostName(name)

    @contextmanager
    def _notify_on_connection_error(self) -> Iterator[None]:
        """Default: no provider to notify. Overridden by Host subclass."""
        yield

    def _ensure_connected(self) -> None:
        """Ensure the pyinfra host is connected."""
        try:
            if not self.connector.host.connected:
                self.connector.host.connect(raise_exceptions=True)
        except ConnectError as e:
            if "authentication error" in str(e).lower():
                raise HostAuthenticationError(f"Authentication failed when connecting to host: {e}") from e
            else:
                raise HostConnectionError(f"Failed to connect to host: {e}") from e

    def _close_paramiko_client(self) -> None:
        """Close the paramiko SSH client if one exists.

        Safe to call on local connectors (no paramiko client) and on
        already-closed clients.
        """
        try:
            client = self.connector.host.connector.client  # ty: ignore[unresolved-attribute]
        except AttributeError:
            return
        if client is not None:
            try:
                client.close()
            except (OSError, SSHException):
                pass

    def disconnect(self) -> None:
        """Disconnect the pyinfra host if connected."""
        self._close_paramiko_client()
        if self.connector.host.connected:
            self.connector.host.disconnect()
            logger.trace("Disconnected pyinfra host {}", self.id)
        self._explicitly_disconnected = True

    def __del__(self) -> None:
        """Best-effort cleanup of the paramiko SSH client on garbage collection."""
        if self._explicitly_disconnected:
            return
        try:
            self._close_paramiko_client()
        except (OSError, SSHException, AttributeError, TypeError):
            logger.debug("Failed to close paramiko client during OuterHost.__del__ for {}", self.id)

    def _run_shell_command(
        self,
        command: StringCommand,
        *,
        _timeout: int | None = None,
        _success_exit_codes: tuple[int, ...] | None = None,
        _env: dict[str, str] | None = None,
        _chdir: str | None = None,
        _shell_executable: str = "sh",
    ) -> tuple[bool, CommandOutput]:
        """Execute a shell command on the host."""
        if self.is_local:
            return self._run_shell_command_local(
                command,
                _timeout=_timeout,
                _success_exit_codes=_success_exit_codes,
                _env=_env,
                _chdir=_chdir,
                _shell_executable=_shell_executable,
            )
        pyinfra_kwargs: dict[str, Any] = {
            "_timeout": _timeout,
            "_success_exit_codes": _success_exit_codes,
            "_env": _env,
            "_chdir": _chdir,
            "_shell_executable": _shell_executable,
        }
        with self._notify_on_connection_error():
            try:
                return self._run_shell_command_with_transient_retry(command, pyinfra_kwargs)
            except OSError as e:
                if "Socket is closed" in str(e):
                    raise HostConnectionError("Connection was closed while running command") from e
                else:
                    raise
            except (EOFError, SSHException) as e:
                raise HostConnectionError("Could not execute command due to connection error") from e

    @_retry_on_transient_ssh_error
    def _run_shell_command_with_transient_retry(
        self,
        command: StringCommand,
        pyinfra_kwargs: dict[str, Any],
    ) -> tuple[bool, CommandOutput]:
        """Inner retry loop for _run_shell_command."""
        self._ensure_connected()
        transport_before = _get_ssh_transport(self.connector.host)
        try:
            result = self.connector.host.run_shell_command(command, **pyinfra_kwargs)
        except ChannelException as e:
            logger.debug("Channel open refused while running command: {}, retrying without disconnect", e)
            raise
        except SSHException as e:
            if "Channel closed" in str(e):
                logger.debug("Channel closed while running command: {}, retrying without disconnect", e)
            else:
                logger.debug("SSH error while running command: {}, disconnecting for retry", e)
                self.connector.host.disconnect()
            raise
        except EOFError as e:
            logger.debug("SSH error while running command: {}, disconnecting for retry", e)
            self.connector.host.disconnect()
            raise
        except OSError as e:
            if "Socket is closed" in str(e):
                logger.debug("Socket closed while running command, disconnecting for retry")
                self.connector.host.disconnect()
            raise

        success, _output = result
        if not success and transport_before is not None and not transport_before.is_active():
            logger.debug("Command failed and SSH transport is dead, disconnecting for retry")
            self.connector.host.disconnect()
            raise SSHException(
                "Command returned failure with dead SSH transport "
                "(likely channel closed during execution by concurrent disconnect)"
            )

        return result

    def _run_shell_command_local(
        self,
        command: StringCommand,
        *,
        _timeout: int | None,
        _success_exit_codes: tuple[int, ...] | None,
        _env: dict[str, str] | None,
        _chdir: str | None,
        _shell_executable: str,
    ) -> tuple[bool, CommandOutput]:
        """Run a shell command on the local machine without going through pyinfra."""
        full_env: dict[str, str] | None = None
        if _env is not None:
            full_env = {**os.environ, **_env}
        cwd_path = Path(_chdir) if _chdir is not None else None
        finished = self.mngr_ctx.concurrency_group.run_process_to_completion(
            [_shell_executable, "-c", command.get_raw_value()],
            timeout=float(_timeout) if _timeout is not None else None,
            is_checked_after=False,
            cwd=cwd_path,
            env=full_env,
        )
        success_codes: tuple[int, ...] = _success_exit_codes if _success_exit_codes else (0,)
        success = finished.returncode in success_codes

        lines: list[OutputLine] = []
        for buffer_name, raw in (("stdout", finished.stdout), ("stderr", finished.stderr)):
            if not raw:
                continue
            text = raw[:-1] if raw.endswith("\n") else raw
            for line in text.split("\n"):
                lines.append(OutputLine(buffer_name=buffer_name, line=line))
        return success, CommandOutput(lines)

    def _get_paramiko_transport(self) -> object:
        """Get the paramiko Transport from the SSH connector."""
        try:
            client = self.connector.host.connector.client  # ty: ignore[unresolved-attribute]
            transport = client.get_transport()
        except AttributeError as e:
            raise HostConnectionError(f"Host does not support SSH file transfer: {e}") from e
        if transport is None:
            raise HostConnectionError("No active SSH transport")
        return transport

    def _create_sftp_client(self, transport: object) -> SFTPClient | None:
        """Create an SFTPClient from a paramiko Transport."""
        return SFTPClient.from_transport(transport)

    def _get_file(
        self,
        remote_filename: str,
        filename_or_io: str | IO[bytes],
        remote_temp_filename: str | None = None,
    ) -> bool:
        """Read a file from the host. Raises FileNotFoundError if not found."""
        with self._notify_on_connection_error():
            try:
                return self._get_file_with_transient_retry(remote_filename, filename_or_io, remote_temp_filename)
            except OSError as e:
                if "Socket is closed" in str(e):
                    raise HostConnectionError("Connection was closed while reading file") from e
                raise
            except (EOFError, SSHException) as e:
                raise HostConnectionError("Could not read file due to connection error") from e

    @_retry_on_transient_ssh_error
    def _get_file_with_transient_retry(
        self,
        remote_filename: str,
        filename_or_io: str | IO[bytes],
        remote_temp_filename: str | None = None,
    ) -> bool:
        self._ensure_connected()
        if not isinstance(filename_or_io, str):
            filename_or_io.seek(0)
            filename_or_io.truncate(0)
        try:
            if not self.is_local:
                return self._get_file_via_paramiko(remote_filename, filename_or_io)
            return self.connector.host.get_file(
                remote_filename,
                filename_or_io,
                remote_temp_filename=remote_temp_filename,
            )
        except OSError as e:
            error_msg = str(e)
            if "No such file or directory" in error_msg or "cannot stat" in error_msg:
                raise FileNotFoundError(f"File not found: {remote_filename}") from e
            elif "Socket is closed" in error_msg:
                logger.debug("Socket closed while reading {}, disconnecting for retry", remote_filename)
                self.connector.host.disconnect()
                raise
            else:
                raise
        except ChannelException as e:
            logger.debug("Channel open refused while reading {}: {}, retrying without disconnect", remote_filename, e)
            raise
        except SSHException as e:
            if "Channel closed" in str(e):
                logger.debug("Channel closed while reading {}: {}, retrying without disconnect", remote_filename, e)
            else:
                logger.debug("SSH error while reading {}: {}, disconnecting for retry", remote_filename, e)
                self.connector.host.disconnect()
            raise
        except EOFError as e:
            logger.debug("SSH error while reading {}: {}, disconnecting for retry", remote_filename, e)
            self.connector.host.disconnect()
            raise

    def _get_file_via_paramiko(
        self,
        remote_filename: str,
        filename_or_io: str | IO[bytes],
    ) -> bool:
        """Download a file using a dedicated paramiko SFTP channel.

        Creates a fresh SFTPClient from the shared SSH transport for each call.
        This is thread-safe because paramiko transports can multiplex channels.
        """
        transport = self._get_paramiko_transport()
        sftp = self._create_sftp_client(transport)
        if sftp is None:
            raise HostConnectionError("Failed to create SFTP channel from transport")
        try:
            if isinstance(filename_or_io, str):
                sftp.get(remote_filename, filename_or_io)
            else:
                sftp.getfo(remote_filename, filename_or_io)
            return True
        except IOError as e:
            error_msg = str(e)
            if "No such file" in error_msg or "not found" in error_msg.lower():
                raise FileNotFoundError(f"File not found: {remote_filename}") from e
            raise
        finally:
            sftp.close()

    def _put_file(
        self,
        filename_or_io: str | IO[str] | IO[bytes],
        remote_filename: str,
        remote_temp_filename: str | None = None,
    ) -> bool:
        """Write a file to the host."""
        with self._notify_on_connection_error():
            try:
                return self._put_file_with_transient_retry(filename_or_io, remote_filename, remote_temp_filename)
            except OSError as e:
                if "Socket is closed" in str(e):
                    raise HostConnectionError("Connection was closed while writing file") from e
                raise
            except (EOFError, SSHException) as e:
                raise HostConnectionError("Could not write file due to connection error") from e

    @_retry_on_transient_ssh_error
    def _put_file_with_transient_retry(
        self,
        filename_or_io: str | IO[str] | IO[bytes],
        remote_filename: str,
        remote_temp_filename: str | None = None,
    ) -> bool:
        self._ensure_connected()
        if not isinstance(filename_or_io, str):
            filename_or_io.seek(0)
        try:
            if not self.is_local:
                return self._put_file_via_paramiko(filename_or_io, remote_filename)
            return self.connector.host.put_file(
                filename_or_io,
                remote_filename,
                remote_temp_filename=remote_temp_filename,
            )
        except OSError as e:
            if "Socket is closed" in str(e):
                logger.debug("Socket closed while writing {}, disconnecting for retry", remote_filename)
                self.connector.host.disconnect()
                raise
            else:
                raise
        except ChannelException as e:
            logger.debug("Channel open refused while writing {}: {}, retrying without disconnect", remote_filename, e)
            raise
        except SSHException as e:
            if "Channel closed" in str(e):
                logger.debug("Channel closed while writing {}: {}, retrying without disconnect", remote_filename, e)
            else:
                logger.debug("SSH error while writing {}: {}, disconnecting for retry", remote_filename, e)
                self.connector.host.disconnect()
            raise
        except EOFError as e:
            logger.debug("SSH error while writing {}: {}, disconnecting for retry", remote_filename, e)
            self.connector.host.disconnect()
            raise

    def _put_file_via_paramiko(
        self,
        filename_or_io: str | IO[str] | IO[bytes],
        remote_filename: str,
    ) -> bool:
        """Upload a file using a dedicated paramiko SFTP channel.

        Creates a fresh SFTPClient from the shared SSH transport for each call.
        This is thread-safe because paramiko transports can multiplex channels.
        """
        transport = self._get_paramiko_transport()
        sftp = self._create_sftp_client(transport)
        if sftp is None:
            raise HostConnectionError("Failed to create SFTP channel from transport")
        try:
            if isinstance(filename_or_io, str):
                sftp.put(filename_or_io, remote_filename)
            else:
                sftp.putfo(filename_or_io, remote_filename)
            return True
        finally:
            sftp.close()

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a command and return the result."""
        logger.trace("Executing command on outer host {}: {}", self.id, command)
        if user is not None:
            raise NotImplementedError("OuterHost does not support su user; pass an SSH user via the connector instead")
        success, output = self._run_shell_command(
            StringCommand(command),
            _chdir=str(cwd) if cwd else None,
            _env=dict(env) if env else None,
            _timeout=int(timeout_seconds) if timeout_seconds else None,
        )
        return CommandResult(stdout=output.stdout, stderr=output.stderr, success=success)

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a stateful command (currently delegates to execute_idempotent_command)."""
        return self.execute_idempotent_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a command, streaming each output line to ``on_line`` as it arrives.

        For local outers, runs through ``ConcurrencyGroup.run_process_to_completion``'s
        ``on_output`` callback (already line-streamed). For SSH outers, bypasses
        pyinfra (which buffers) and uses paramiko's ``exec_command`` directly:
        stdout is read line-by-line in this thread, and a daemon thread reads
        stderr in parallel.

        The command is treated as **idempotent**: transient SSH errors (socket
        closed, channel closed, EOF) trigger a retry with backoff. When a retry
        fires, ``on_line`` is re-called with the new attempt's output -- callers
        should expect duplicate lines on retry. Use this only for commands like
        ``docker build`` where re-running from scratch is safe.
        """
        if self.is_local:
            return self._execute_streaming_local(command, on_line, env, timeout_seconds)
        with self._notify_on_connection_error():
            try:
                return self._execute_streaming_ssh_with_retry(command, on_line, env, timeout_seconds)
            except OSError as e:
                if "Socket is closed" in str(e):
                    raise HostConnectionError("Connection was closed during streaming command") from e
                raise
            except (EOFError, SSHException) as e:
                raise HostConnectionError("Could not execute streaming command due to connection error") from e

    def _execute_streaming_local(
        self,
        command: str,
        on_line: Callable[[str], None],
        env: Mapping[str, str] | None,
        timeout_seconds: float | None,
    ) -> CommandResult:
        """Local-process streaming via the concurrency group's on_output callback."""
        full_env: dict[str, str] | None = None
        if env is not None:
            full_env = {**os.environ, **env}
        accumulator = _StreamingOutputAccumulator(on_line=on_line)
        finished = self.mngr_ctx.concurrency_group.run_process_to_completion(
            ["sh", "-c", command],
            timeout=timeout_seconds,
            is_checked_after=False,
            env=full_env,
            on_output=accumulator,
        )
        return CommandResult(
            stdout=accumulator.stdout,
            stderr=accumulator.stderr,
            success=(finished.returncode == 0),
        )

    @_retry_on_transient_ssh_error
    def _execute_streaming_ssh_with_retry(
        self,
        command: str,
        on_line: Callable[[str], None],
        env: Mapping[str, str] | None,
        timeout_seconds: float | None,
    ) -> CommandResult:
        """SSH-channel streaming via paramiko's exec_command (bypasses pyinfra's buffering).

        Wrapped with the standard transient-SSH-error retry decorator. On retry,
        ``on_line`` is called again with the new attempt's output.
        """
        self._ensure_connected()
        client = self.connector.host.connector.client  # ty: ignore[unresolved-attribute]
        if client is None:
            raise HostConnectionError("No SSH client available for streaming")

        # paramiko's exec_command env= is unreliable across servers (sshd's
        # AcceptEnv usually rejects it), so we prepend env vars to the command
        # instead. Same approach used elsewhere in mngr.
        if env:
            env_prefix = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items())
            full_command = f"{env_prefix} {command}"
        else:
            full_command = command

        try:
            stdin, stdout, stderr = client.exec_command(
                full_command,
                timeout=timeout_seconds,
                get_pty=False,
            )
        except (ChannelException, SSHException, EOFError, OSError) as e:
            logger.debug("SSH error opening streaming channel: {}, disconnecting for retry", e)
            self.connector.host.disconnect()
            raise

        stdin.close()

        stdout_lines: list[str] = []
        stderr_state = _SSHStderrState(stderr=stderr, on_line=on_line)
        stderr_thread = threading.Thread(target=_drain_ssh_stderr_into, args=(stderr_state,), daemon=True)
        stderr_thread.start()

        try:
            for raw in iter(stdout.readline, ""):
                stripped = raw.rstrip("\n")
                stdout_lines.append(stripped)
                on_line(stripped)
        except (OSError, SSHException, EOFError) as e:
            logger.debug("stdout reader stopped on error: {}, disconnecting for retry", e)
            self.connector.host.disconnect()
            raise

        # Drain stderr thread; paramiko's stream typically EOFs around the same
        # time as stdout, so the join should be fast.
        stderr_thread.join(timeout=10.0)

        try:
            exit_code = stdout.channel.recv_exit_status()
        except (OSError, SSHException) as e:
            logger.debug("recv_exit_status failed: {}, disconnecting for retry", e)
            self.connector.host.disconnect()
            raise

        return CommandResult(
            stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
            stderr="\n".join(stderr_state.lines) + ("\n" if stderr_state.lines else ""),
            success=(exit_code == 0),
        )

    def read_file(self, path: Path) -> bytes:
        """Read a file and return its contents as bytes."""
        if self.is_local:
            return path.read_bytes()
        else:
            output = io.BytesIO()
            self._get_file(str(path), output)
            return output.getvalue()

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        """Write bytes content to a file, creating parent directories as needed."""
        if is_atomic:
            write_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        else:
            write_path = path

        if self.is_local:
            try:
                write_path.write_bytes(content)
            except FileNotFoundError:
                write_path.parent.mkdir(parents=True, exist_ok=True)
                write_path.write_bytes(content)
        else:
            try:
                is_success = self._put_file(io.BytesIO(content), str(write_path))
            except IOError:
                is_success = False
            if not is_success:
                parent_dir = str(write_path.parent)
                result = self.execute_idempotent_command(f"mkdir -p '{parent_dir}'")
                if not result.success:
                    raise MngrError(
                        f"Failed to create parent directory '{parent_dir}' on outer host {self.id} because: {result.stderr}"
                    )
                is_success = self._put_file(io.BytesIO(content), str(write_path))
                if not is_success:
                    raise MngrError(f"Failed to write file '{str(write_path)}' on outer host {self.id}'")
        if write_path != path:
            result = self.execute_idempotent_command(f"mv '{str(write_path)}' '{str(path)}'")
            if not result.success:
                raise MngrError(
                    f"Failed to move temp file to final location on outer host {self.id} because: {result.stderr}"
                )
        if mode is not None:
            self.execute_idempotent_command(f"chmod {mode} '{str(path)}'")

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        """Read a file and return its contents as a string."""
        return self.read_file(path).decode(encoding)

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        """Write string content to a file, creating parent directories as needed."""
        self.write_file(path, content.encode(encoding), mode=mode)

    def _get_file_mtime(self, path: Path) -> datetime | None:
        """Get the mtime of a file on the host."""
        if self.is_local:
            try:
                mtime = path.stat().st_mtime
                return datetime.fromtimestamp(mtime, tz=timezone.utc)
            except (FileNotFoundError, OSError):
                return None
        result = self.execute_idempotent_command(
            f"stat -c %Y '{str(path)}' 2>/dev/null || stat -f %m '{str(path)}' 2>/dev/null"
        )
        if result.success and result.stdout.strip():
            try:
                mtime = int(result.stdout.strip())
                return datetime.fromtimestamp(mtime, tz=timezone.utc)
            except ValueError:
                pass
        return None

    def get_file_mtime(self, path: Path) -> datetime | None:
        """Return the modification time of a file, or None if the file doesn't exist."""
        return self._get_file_mtime(path)

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        """Get SSH connection info for this host if it's remote."""
        if self.is_local:
            return None

        host_data = self.connector.host.data
        user = host_data.get("ssh_user", "root")
        hostname = self.connector.host.name
        port = host_data.get("ssh_port", 22)
        key_path_str = host_data.get("ssh_key", "")
        if not key_path_str:
            return (user, hostname, port, Path(""))

        return (user, hostname, port, Path(key_path_str))
