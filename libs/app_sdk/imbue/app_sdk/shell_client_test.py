import socket
from threading import Thread

import pytest

from imbue.app_sdk.errors import ShellUnreachableError
from imbue.app_sdk.shell_client import SHELL_URL_ENVIRONMENT_VARIABLE
from imbue.app_sdk.shell_client import fetch_from_shell

_HALF_AN_ANSWER = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nhalf"


def _answer_half_and_hang_up(listener: socket.socket) -> None:
    connection, _address = listener.accept()
    with connection:
        connection.recv(65536)
        connection.sendall(_HALF_AN_ANSWER)


def test_an_answer_the_shell_cuts_short_is_a_shell_that_did_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        thread = Thread(target=_answer_half_and_hang_up, args=(listener,), daemon=True)
        thread.start()
        monkeypatch.setenv(SHELL_URL_ENVIRONMENT_VARIABLE, f"http://127.0.0.1:{listener.getsockname()[1]}")

        with pytest.raises(ShellUnreachableError, match="/api/inventory"):
            fetch_from_shell("/api/inventory")

        thread.join()
