"""
warm_cli - Pre-warmed Python CLI runner

Each invocation leaves behind a fresh, pre-imported successor process
that waits for the next call. No long-lived daemon, no fork-after-threads.

The trick: instead of proxying I/O, the client passes its actual
stdin/stdout/stderr file descriptors via SCM_RIGHTS over a Unix socket.
The warm process dup2()s them onto 0/1/2 and runs directly on the caller's
terminal. The client just waits for an exit code.

Usage:
    import click
    from warm_cli import warm_cli

    @click.command()
    @click.argument("name")
    def hello(name):
        click.echo(f"Hello, {name}!")

    if __name__ == "__main__":
        warm_cli(hello)
"""

import array
import json
import os
import signal
import socket
import struct
import sys
import traceback

DEFAULT_TIMEOUT = 3600  # 1 hour


# --- SCM_RIGHTS helpers ------------------------------------------------------


def _send_fds(sock, fds, data=b"\x00"):
    """Send file descriptors over a Unix socket using SCM_RIGHTS."""
    fd_array = array.array("i", fds)
    sock.sendmsg(
        [data],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fd_array)],
    )


def _recv_fds(sock, num_fds):
    """Receive file descriptors and data from a Unix socket."""
    fd_size = num_fds * array.array("i").itemsize
    data, ancdata, _, _ = sock.recvmsg(
        65536,
        socket.CMSG_LEN(fd_size),
    )
    fds = []
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fd_array = array.array("i")
            fd_array.frombytes(cmsg_data[:fd_size])
            fds = list(fd_array)
    return data, fds


# --- Warm server (one-shot) --------------------------------------------------


def _warm_server(entry_module, entry_func_name, socket_path, timeout):
    """
    Bind, wait for ONE connection, take over the client's terminal, run,
    spawn a replacement, exit.
    """
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(socket_path)
    except OSError:
        return
    sock.listen(1)
    sock.settimeout(timeout)

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        conn, _ = sock.accept()
    except socket.timeout:
        sock.close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        return

    sock.close()

    # Receive client's file descriptors and payload in one recvmsg call
    raw_payload, fds = _recv_fds(conn, 3)

    if len(fds) < 3:
        conn.close()
        return

    client_stdin, client_stdout, client_stderr = fds

    # Read any remaining payload data (in case it was larger than one recv)
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

    payload = json.loads(b"".join(chunks))

    # Spawn replacement BEFORE we do any work
    _spawn_warm(entry_module, entry_func_name, socket_path, timeout)

    # Take over the client's terminal
    os.dup2(client_stdin, 0)
    os.dup2(client_stdout, 1)
    os.dup2(client_stderr, 2)
    os.close(client_stdin)
    os.close(client_stdout)
    os.close(client_stderr)

    # Restore signal handling now that we own a terminal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Set up environment to match the caller
    os.environ.clear()
    os.environ.update(payload.get("env", {}))
    sys.argv = payload.get("argv", [])
    if "cwd" in payload:
        try:
            os.chdir(payload["cwd"])
        except OSError as e:
            # Don't silently run in the wrong directory: relative paths would
            # resolve against the warm process's cwd instead of the caller's.
            # Warn and continue so the CLI still runs (the caller's cwd may have
            # been deleted), but the discrepancy is visible.
            print(f"warm_cli: could not chdir to {payload['cwd']!r}: {e}", file=sys.stderr)

    # Reattach Python-level stdio to the new FDs
    sys.stdin = open(0, "r", closefd=False)
    sys.stdout = open(1, "w", closefd=False)
    sys.stderr = open(2, "w", closefd=False)

    # Run the CLI
    mod = sys.modules[entry_module]
    func = getattr(mod, entry_func_name)

    exit_code = 0
    try:
        result = func(standalone_mode=False)
        if isinstance(result, int):
            exit_code = result
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    except Exception:
        # Deliberate process-entrypoint boundary: this is the top-level CLI runner,
        # so it mirrors a normal entrypoint (print the traceback, exit non-zero).
        traceback.print_exc()
        exit_code = 1

    # Send exit code back to the waiting client
    try:
        conn.sendall(struct.pack("!i", exit_code))
    except BrokenPipeError:
        pass
    conn.close()


# --- Double-fork to detach ---------------------------------------------------


def _spawn_warm(entry_module, entry_func_name, socket_path, timeout):
    """Fork a fully detached warm process. Returns immediately in parent."""
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        return

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    os.setsid()

    devnull_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull_fd, 0)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    if devnull_fd > 2:
        os.close(devnull_fd)

    _warm_server(entry_module, entry_func_name, socket_path, timeout)
    os._exit(0)


# --- Client ------------------------------------------------------------------


def _client_invoke(socket_path):
    """Connect, hand over our FDs, wait for exit code."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path)

    payload = json.dumps(
        {
            "argv": sys.argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
        }
    ).encode("utf-8")

    # Send our stdin/stdout/stderr FDs along with the payload
    _send_fds(sock, [0, 1, 2], data=payload)

    # Nothing to do but wait for the exit code
    data = b""
    while len(data) < 4:
        chunk = sock.recv(4 - len(data))
        if not chunk:
            return 1
        data += chunk

    sock.close()
    return struct.unpack("!i", data)[0]


# --- Public API --------------------------------------------------------------


def warm_cli(func, socket_path=None, timeout=DEFAULT_TIMEOUT):
    """
    Drop-in replacement for your CLI entry point.

    Instead of:
        if __name__ == "__main__":
            my_click_command()

    Use:
        if __name__ == "__main__":
            warm_cli(my_click_command)
    """
    if socket_path is None:
        name = f"{func.__module__}.{func.__name__}"
        socket_path = f"/tmp/warm_cli_{name}_{os.getuid()}.sock"

    entry_module = func.__module__
    entry_func_name = func.__name__

    # Try the warm path
    try:
        exit_code = _client_invoke(socket_path)
        sys.exit(exit_code)
    except (ConnectionRefusedError, FileNotFoundError, ConnectionResetError):
        pass

    # Cold path: run directly
    exit_code = 0
    try:
        result = func(standalone_mode=False)
        if isinstance(result, int):
            exit_code = result
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    except Exception:
        # Deliberate process-entrypoint boundary: this is the top-level CLI runner,
        # so it mirrors a normal entrypoint (print the traceback, exit non-zero).
        traceback.print_exc()
        exit_code = 1

    # Spawn warm successor
    _spawn_warm(entry_module, entry_func_name, socket_path, timeout)

    sys.exit(exit_code)
