import array
import json
import os
import signal
import socket
import struct
import sys
import traceback
import types
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click

_DEFAULT_TIMEOUT_SECONDS: Final[int] = 3600
_EXPECTED_FD_COUNT: Final[int] = 3


def _send_fds(sock: socket.socket, fds: list[int], data: bytes = b"\x00") -> None:
    """Send file descriptors over a Unix socket using SCM_RIGHTS."""
    fd_array = array.array("i", fds)
    sock.sendmsg(
        [data],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fd_array)],
    )


def _recv_fds(sock: socket.socket, fd_count: int) -> tuple[bytes, list[int]]:
    """Receive file descriptors and data from a Unix socket."""
    fd_size = fd_count * array.array("i").itemsize
    data, ancdata, _, _ = sock.recvmsg(
        65536,
        socket.CMSG_LEN(fd_size),
    )
    fds: list[int] = []
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fd_array = array.array("i")
            fd_array.frombytes(cmsg_data[:fd_size])
            fds = list(fd_array)
    return data, fds


def _resolve_click_callback(func: Callable[..., object] | click.Command) -> types.FunctionType:
    """Extract the underlying callback from a click command, or return the callable as-is."""
    if isinstance(func, click.Command):
        callback = func.callback
        if callback is None:
            raise TypeError(f"Click command {func.name!r} has no callback function")
    else:
        callback = func
    if not isinstance(callback, types.FunctionType):
        raise TypeError(f"Expected a function, got {type(callback).__name__}")
    return callback


def _run_entry_func(
    func: Callable[..., object] | click.Command,
    args: list[str] | None = None,
) -> int:
    """Run a click command entry function and return an exit code."""
    exit_code = 0
    try:
        result = func(args=args, standalone_mode=False)
        if isinstance(result, int):
            exit_code = result
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    except Exception:
        traceback.print_exc()
        exit_code = 1
    return exit_code


def _accept_one_connection(
    socket_path: Path,
    timeout_seconds: int,
) -> socket.socket | None:
    """Bind a Unix socket, wait for one connection, and return it (or None on timeout)."""
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
    except OSError:
        sock.close()
        return None
    sock.listen(1)
    sock.settimeout(timeout_seconds)

    # Ignore SIGINT while waiting -- only the foreground process should handle it
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        conn, _ = sock.accept()
    except socket.timeout:
        try:
            socket_path.unlink()
        except OSError:
            pass
        return None
    finally:
        sock.close()

    return conn


def _receive_client_payload(conn: socket.socket) -> dict[str, object] | None:
    """Receive file descriptors and JSON payload from the client. Returns None on protocol error."""
    raw_payload, fds = _recv_fds(conn, _EXPECTED_FD_COUNT)

    if len(fds) < _EXPECTED_FD_COUNT:
        return None

    # Drain any remaining payload data that didn't fit in the first recv
    conn.setblocking(False)
    chunks = [raw_payload]
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except BlockingIOError:
        pass
    conn.setblocking(True)

    payload: dict[str, object] = json.loads(b"".join(chunks))
    payload["_fds"] = fds
    return payload


def _take_over_client_terminal(fds: list[int], payload: dict[str, object]) -> None:
    """Redirect stdio to the client's file descriptors and set up the client's environment."""
    client_stdin_fd, client_stdout_fd, client_stderr_fd = fds

    os.dup2(client_stdin_fd, 0)
    os.dup2(client_stdout_fd, 1)
    os.dup2(client_stderr_fd, 2)
    os.close(client_stdin_fd)
    os.close(client_stdout_fd)
    os.close(client_stderr_fd)

    # Restore default signal handling now that we own a terminal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Set up the environment to match the caller
    os.environ.clear()
    os.environ.update(payload.get("env", {}))  # type: ignore[arg-type]
    sys.argv = payload.get("argv", [])  # type: ignore[assignment]
    if "cwd" in payload:
        try:
            os.chdir(str(payload["cwd"]))
        except OSError:
            pass

    # Reattach Python-level stdio to the new file descriptors
    sys.stdin = open(0, "r", closefd=False)  # noqa: SIM115
    sys.stdout = open(1, "w", closefd=False)  # noqa: SIM115
    sys.stderr = open(2, "w", closefd=False)  # noqa: SIM115


def _warm_server(
    entry_module: str,
    entry_func_name: str,
    socket_path: Path,
    timeout_seconds: int,
) -> None:
    """Bind, wait for one connection, take over the client's terminal, run, then spawn a replacement."""
    conn = _accept_one_connection(socket_path, timeout_seconds)
    if conn is None:
        return

    payload = _receive_client_payload(conn)
    if payload is None:
        conn.close()
        return

    fds: list[int] = payload.pop("_fds")  # type: ignore[assignment]

    # Take over the client's terminal, then spawn a replacement.
    # The replacement must be spawned AFTER dup2+close to avoid leaking the client's pipe fds.
    _take_over_client_terminal(fds, payload)
    _spawn_warm_process(entry_module, entry_func_name, socket_path, timeout_seconds)

    # Run the CLI entry function
    mod = sys.modules[entry_module]
    func = getattr(mod, entry_func_name)
    exit_code = _run_entry_func(func)

    # Send exit code back to the waiting client
    try:
        conn.sendall(struct.pack("!i", exit_code))
    except BrokenPipeError:
        pass
    conn.close()


def _spawn_warm_process(
    entry_module: str,
    entry_func_name: str,
    socket_path: Path,
    timeout_seconds: int,
) -> None:
    """Double-fork a fully detached warm server process. Returns immediately in the parent."""
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        return

    # First child: fork again and exit so the grandchild is reparented to init
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild: detach from the controlling terminal
    os.setsid()

    # Close all inherited fds >= 3 to avoid leaking the client's pipe fds
    # (which would prevent EOF detection on the parent's subprocess pipes)
    try:
        max_fd = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        max_fd = 1024
    os.closerange(3, max_fd)

    # Redirect stdio to /dev/null
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull_fd, 0)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    if devnull_fd > 2:
        os.close(devnull_fd)

    _warm_server(entry_module, entry_func_name, socket_path, timeout_seconds)
    os._exit(0)


def _client_invoke(socket_path: Path) -> int:
    """Connect to a warm server, hand over our file descriptors, and wait for the exit code."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))

        payload = json.dumps(
            {
                "argv": sys.argv,
                "env": dict(os.environ),
                "cwd": os.getcwd(),
            }
        ).encode("utf-8")

        # Send stdin/stdout/stderr file descriptors along with the payload
        _send_fds(sock, [0, 1, 2], data=payload)

        # Wait for the exit code from the warm server
        data = b""
        while len(data) < 4:
            chunk = sock.recv(4 - len(data))
            if not chunk:
                return 1
            data += chunk

        return struct.unpack("!i", data)[0]
    finally:
        sock.close()


def _default_socket_path(callback: types.FunctionType) -> Path:
    name = f"{callback.__module__}.{callback.__name__}"
    return Path(f"/tmp/warm_cli_{name}_{os.getuid()}.sock")


def warm_cli(
    func: Callable[..., object] | click.Command,
    socket_path: Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Drop-in wrapper for a click CLI entry point that pre-warms a successor process.

    Each invocation leaves behind a fresh, pre-imported process that waits for the
    next call. The client passes its stdin/stdout/stderr file descriptors via
    SCM_RIGHTS over a Unix socket, so the warm process runs directly on the caller's
    terminal. No long-lived daemon, no fork-after-threads.

    Instead of:
        if __name__ == "__main__":
            my_click_command()

    Use:
        if __name__ == "__main__":
            warm_cli(my_click_command)
    """
    # Resolve the callback function from a click command for socket path derivation
    # and for the warm server to look up the function by module + name
    callback = _resolve_click_callback(func)
    resolved_socket_path = socket_path if socket_path is not None else _default_socket_path(callback)

    entry_module = callback.__module__
    entry_func_name = callback.__name__

    # Try the warm path first
    try:
        exit_code = _client_invoke(resolved_socket_path)
        sys.exit(exit_code)
    except (ConnectionRefusedError, FileNotFoundError, ConnectionResetError):
        pass

    # Cold path: run the function directly
    exit_code = _run_entry_func(func)

    # Spawn a warm successor for the next invocation
    _spawn_warm_process(entry_module, entry_func_name, resolved_socket_path, timeout_seconds)

    sys.exit(exit_code)
