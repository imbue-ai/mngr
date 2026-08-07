import os
import socket
import types
from pathlib import Path
from uuid import uuid4

import click
import pytest

from imbue.imbue_common.warm_cli import _default_socket_path
from imbue.imbue_common.warm_cli import _recv_fds
from imbue.imbue_common.warm_cli import _resolve_click_callback
from imbue.imbue_common.warm_cli import _run_entry_func
from imbue.imbue_common.warm_cli import _send_fds


def test_resolve_click_callback_returns_plain_function_as_is() -> None:
    def my_func() -> None:
        pass

    result = _resolve_click_callback(my_func)

    assert result is my_func


def test_resolve_click_callback_extracts_callback_from_click_command() -> None:
    @click.command()
    def my_command() -> None:
        pass

    result = _resolve_click_callback(my_command)

    assert result is not my_command
    assert isinstance(result, types.FunctionType)
    assert result.__name__ == "my_command"


def test_resolve_click_callback_raises_for_command_without_callback() -> None:
    cmd = click.Command(name="empty")

    with pytest.raises(TypeError, match="has no callback function"):
        _resolve_click_callback(cmd)


def test_run_entry_func_returns_zero_for_successful_click_command() -> None:
    @click.command()
    def success_cmd() -> None:
        pass

    exit_code = _run_entry_func(success_cmd, args=[])

    assert exit_code == 0


def test_run_entry_func_returns_int_result_from_callback() -> None:
    @click.command()
    def returns_42() -> int:
        return 42

    exit_code = _run_entry_func(returns_42, args=[])

    assert exit_code == 42


def test_run_entry_func_returns_exit_code_from_system_exit() -> None:
    @click.command()
    def exits_with_3() -> None:
        raise SystemExit(3)

    exit_code = _run_entry_func(exits_with_3, args=[])

    assert exit_code == 3


def test_run_entry_func_returns_one_for_exception() -> None:
    @click.command()
    def raises_value_error() -> None:
        raise ValueError("something went wrong")

    exit_code = _run_entry_func(raises_value_error, args=[])

    assert exit_code == 1


def test_default_socket_path_uses_module_and_function_name() -> None:
    def my_func() -> None:
        pass

    path = _default_socket_path(my_func)

    assert "warm_cli" in str(path)
    assert "my_func" in str(path)
    assert str(path).startswith("/tmp/")
    assert str(path).endswith(".sock")


def test_send_and_recv_fds_round_trips_file_descriptors() -> None:
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = Path(f"/tmp/wc_fds_{uuid4().hex[:12]}.sock")
    try:
        server_sock.bind(str(socket_path))
        server_sock.listen(1)

        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_sock.connect(str(socket_path))

        conn, _ = server_sock.accept()

        # Send a pair of pipe fds
        read_fd, write_fd = os.pipe()
        _send_fds(client_sock, [read_fd, write_fd], data=b"hello")

        data, received_fds = _recv_fds(conn, 2)

        assert data == b"hello"
        assert len(received_fds) == 2

        # Verify the received fds are functional: write through one, read from the other
        os.write(received_fds[1], b"test data")
        os.close(received_fds[1])
        result = os.read(received_fds[0], 100)
        assert result == b"test data"

        os.close(received_fds[0])
        os.close(read_fd)
        os.close(write_fd)
        client_sock.close()
        conn.close()
    finally:
        server_sock.close()
        socket_path.unlink(missing_ok=True)
