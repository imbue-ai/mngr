#!/usr/bin/env python3
"""Calls a codex agent's ``codex app-server`` from the agent's host.

By default it rewrites the agent's ``background_tasks/task-*`` records from what the app-server lists. With
``--trust-hooks`` it trusts the hooks codex lists as new or changed in the current directory, as the "Trust all and
continue" of codex's hook review does.

Standard library only, for Python 3.9 and later: it runs under the ``python3`` of the agent's host.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import itertools
import json
import os
import re
import socket
import struct
import time
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from typing import Any
from typing import BinaryIO
from typing import Final
from typing import TypedDict
from urllib.parse import quote

RECORD_DIR_NAME: Final[str] = "background_tasks"
RECORD_NAME_PREFIX: Final[str] = "task-"
SHELL_KIND: Final[str] = "SHELL"
SUBAGENT_KIND: Final[str] = "SUBAGENT"
BACKGROUND_TERMINAL_NATIVE_TYPE: Final[str] = "backgroundTerminals"
DEFAULT_TIME_LIMIT_SECONDS: Final[float] = 5.0
# How ``--trust-hooks`` starts its outcome once it has trusted every hook codex would review.
HOOKS_TRUSTED_OUTCOME_PREFIX: Final[str] = "trusted:"

_PLUGIN_DIR_PARTS: Final[tuple[str, ...]] = ("plugin", "codex")
_CODEX_HOME_DIR_NAME: Final[str] = "home"
_LOCK_FILE_NAME: Final[str] = ".record_background_tasks.lock"
_SOCKET_ROOT: Final[str] = "/tmp"
_SOCKET_DIR_PREFIX: Final[str] = "mngr-codex-"
_SOCKET_FILE_NAME: Final[str] = "app-server.sock"
_CLIENT_NAME: Final[str] = "mngr"
_TURN_OPEN_STATUS: Final[str] = "active"
# The ``trustStatus`` values of the hooks codex's review lists.
_HOOK_REVIEW_STATUSES: Final[frozenset[str]] = frozenset({"untrusted", "modified"})
_RECORD_NAME_DISALLOWED: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_PAGES: Final[int] = 100
_MAX_HEADER_LINES: Final[int] = 100
_MAX_HEADER_LINE_BYTES: Final[int] = 8192
_MAX_MESSAGE_BYTES: Final[int] = 64 * 1024 * 1024
_WEBSOCKET_ACCEPT_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_CONTINUATION: Final[int] = 0x0
_OPCODE_TEXT: Final[int] = 0x1
_OPCODE_BINARY: Final[int] = 0x2
_OPCODE_CLOSE: Final[int] = 0x8
_OPCODE_PING: Final[int] = 0x9
_OPCODE_PONG: Final[int] = 0xA

RecordFields = tuple[str, str]


class AppServerGoneError(Exception):
    """Nothing listens on the app-server's socket."""


class AppServerReadError(Exception):
    """A read of the app-server failed, or it answered in a shape this writer does not know."""


class CodexThread(TypedDict):
    """One loaded thread, as this writer reads it."""

    thread_id: str
    parent_thread_id: str | None
    thread_source: str | None
    is_turn_open: bool
    background_terminal_ids: tuple[str, ...]


class _Connection(TypedDict):
    sock: socket.socket
    reader: BinaryIO
    request_ids: Iterator[int]
    deadline: float


def app_server_socket_path(state_dir: Path) -> Path:
    """The unix socket the agent at ``state_dir`` runs its app-server on, e.g. ``/tmp/mngr-codex-<hash>/app-server.sock``."""
    codex_home = state_dir.joinpath(*_PLUGIN_DIR_PARTS, _CODEX_HOME_DIR_NAME)
    digest = hashlib.sha1(str(codex_home).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return Path(_SOCKET_ROOT) / f"{_SOCKET_DIR_PREFIX}{digest}" / _SOCKET_FILE_NAME


def pass_lock_path(state_dir: Path) -> Path:
    """The file a pass locks for the agent at ``state_dir``."""
    return state_dir.joinpath(*_PLUGIN_DIR_PARTS, _LOCK_FILE_NAME)


def record_name(task_id: str) -> str:
    return RECORD_NAME_PREFIX + _RECORD_NAME_DISALLOWED.sub("_", task_id)


def plan_records(
    threads: Sequence[CodexThread], existing_names: Set[str], ended_thread_id: str | None = None
) -> dict[str, RecordFields]:
    """The records codex's listing yields, as ``{name: (kind, native_type)}``, with ``ended_thread_id``'s turn over.

    A top-level thread with a turn open adds no terminal record, since codex lists that turn's own commands among them.
    """
    records: dict[str, RecordFields] = {}
    for thread in threads:
        is_turn_open = thread["is_turn_open"] and thread["thread_id"] != ended_thread_id
        if thread["parent_thread_id"] is not None and is_turn_open:
            records[record_name(thread["thread_id"])] = (SUBAGENT_KIND, thread["thread_source"] or "")
        else:
            for terminal_id in thread["background_terminal_ids"]:
                name = record_name(terminal_id)
                if not is_turn_open or name in existing_names:
                    records[name] = (SHELL_KIND, BACKGROUND_TERMINAL_NATIVE_TYPE)
    return records


def existing_record_names(record_dir: Path) -> frozenset[str]:
    try:
        entries = list(os.scandir(record_dir))
    except FileNotFoundError:
        return frozenset()
    return frozenset(entry.name for entry in entries if entry.name.startswith(RECORD_NAME_PREFIX) and entry.is_file())


def write_records(record_dir: Path, records: Mapping[str, RecordFields], now: int) -> None:
    """Write each record as seen at ``now``, then remove this writer's records that are not among them."""
    if records:
        record_dir.mkdir(parents=True, exist_ok=True)
    for name, (kind, native_type) in records.items():
        line = f"seen:{now} kind:{kind}"
        if native_type:
            line = f"{line} native_type:{quote(native_type, safe='')}"
        staging_path = record_dir / f".{name}.{os.getpid()}.tmp"
        staging_path.write_text(line + "\n", encoding="utf-8")
        os.replace(staging_path, record_dir / name)
    for name in existing_record_names(record_dir).difference(records):
        (record_dir / name).unlink(missing_ok=True)


def _connect(socket_path: Path, time_limit_seconds: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(time_limit_seconds)
    try:
        sock.connect(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError) as error:
        sock.close()
        raise AppServerGoneError(f"nothing listens on {socket_path}") from error
    except OSError as error:
        sock.close()
        raise AppServerReadError(f"could not connect to {socket_path}: {error}") from error
    return sock


def _read_exactly(reader: BinaryIO, size: int) -> bytes:
    data = reader.read(size)
    if data is None or len(data) != size:
        raise AppServerReadError("the app-server closed the connection")
    return data


def _open_websocket(sock: socket.socket, reader: BinaryIO) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall(
        (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
    )
    status_line = reader.readline(_MAX_HEADER_LINE_BYTES)
    if status_line.split(b" ")[1:2] != [b"101"]:
        raise AppServerReadError(f"the app-server refused the WebSocket upgrade: {status_line!r}")
    headers: dict[bytes, bytes] = {}
    for _ in range(_MAX_HEADER_LINES):
        line = reader.readline(_MAX_HEADER_LINE_BYTES)
        if not line:
            raise AppServerReadError("the app-server closed the connection during the upgrade")
        if not line.strip():
            break
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    else:
        raise AppServerReadError("the app-server's upgrade headers did not end")
    accept_digest = hashlib.sha1((key + _WEBSOCKET_ACCEPT_GUID).encode("ascii"), usedforsecurity=False).digest()
    if headers.get(b"sec-websocket-accept") != base64.b64encode(accept_digest):
        raise AppServerReadError("the app-server's upgrade answered another key")


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


def _receive_message(connection: _Connection) -> str:
    """The next text message, answering any ping on the way."""
    fragments: list[bytes] = []
    is_complete = False
    while not is_complete:
        if time.monotonic() > connection["deadline"]:
            raise AppServerReadError("no whole message within the time limit")
        first, second = _read_exactly(connection["reader"], 2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", _read_exactly(connection["reader"], 2))
        elif length == 127:
            (length,) = struct.unpack("!Q", _read_exactly(connection["reader"], 8))
        else:
            pass
        if length > _MAX_MESSAGE_BYTES:
            raise AppServerReadError(f"a frame of {length} bytes")
        mask = _read_exactly(connection["reader"], 4) if second & 0x80 else b""
        payload = _read_exactly(connection["reader"], length)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode in (_OPCODE_TEXT, _OPCODE_BINARY, _OPCODE_CONTINUATION):
            fragments.append(payload)
            is_complete = bool(first & 0x80)
        elif opcode == _OPCODE_PING:
            _send_frame(connection["sock"], _OPCODE_PONG, payload)
        elif opcode == _OPCODE_PONG:
            pass
        elif opcode == _OPCODE_CLOSE:
            raise AppServerReadError("the app-server closed the connection")
        else:
            raise AppServerReadError(f"a frame with opcode {opcode}")
    try:
        return b"".join(fragments).decode("utf-8")
    except UnicodeDecodeError as error:
        raise AppServerReadError("a message that is not UTF-8") from error


def _call(connection: _Connection, method: str, params: Mapping[str, Any]) -> Any:
    """The result of one JSON-RPC request, skipping the notifications and requests the app-server sends meanwhile."""
    request_id = next(connection["request_ids"])
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
    _send_frame(connection["sock"], _OPCODE_TEXT, json.dumps(request).encode("utf-8"))
    message: Any = None
    while not (isinstance(message, dict) and message.get("id") == request_id and "method" not in message):
        try:
            message = json.loads(_receive_message(connection))
        except ValueError as error:
            raise AppServerReadError(f"{method}: an answer that is not JSON") from error
    if "result" not in message:
        raise AppServerReadError(f"{method}: {message.get('error')}")
    return message["result"]


def _list_all(connection: _Connection, method: str, params: Mapping[str, Any]) -> list[Any]:
    """Every item of a paged list."""
    items: list[Any] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        result = _call(connection, method, {**params, "cursor": cursor} if cursor is not None else params)
        page = result.get("data") if isinstance(result, dict) else None
        next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if not isinstance(page, list) or not (next_cursor is None or isinstance(next_cursor, str)):
            raise AppServerReadError(f"{method}: an answer this writer does not know")
        items.extend(page)
        if next_cursor is None:
            return items
        cursor = next_cursor
    raise AppServerReadError(f"{method}: more than {_MAX_PAGES} pages")


def _is_optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _read_thread(connection: _Connection, thread_id: str) -> CodexThread:
    result = _call(connection, "thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    if not (
        isinstance(thread, dict)
        and isinstance(status, dict)
        and isinstance(status.get("type"), str)
        and _is_optional_text(thread.get("parentThreadId"))
        and _is_optional_text(thread.get("threadSource"))
    ):
        raise AppServerReadError(f"thread/read: an answer this writer does not know for {thread_id}")
    terminals = _list_all(connection, "thread/backgroundTerminals/list", {"threadId": thread_id})
    if not all(isinstance(terminal, dict) and isinstance(terminal.get("itemId"), str) for terminal in terminals):
        raise AppServerReadError(
            f"thread/backgroundTerminals/list: an answer this writer does not know for {thread_id}"
        )
    return CodexThread(
        thread_id=thread_id,
        parent_thread_id=thread.get("parentThreadId"),
        thread_source=thread.get("threadSource"),
        is_turn_open=status["type"] == _TURN_OPEN_STATUS,
        background_terminal_ids=tuple(terminal["itemId"] for terminal in terminals),
    )


@contextmanager
def _initialized_connection(socket_path: Path, time_limit_seconds: float) -> Generator[_Connection, None, None]:
    """A connection to the app-server past its ``initialize`` handshake, closed on exit."""
    with _connect(socket_path, time_limit_seconds) as sock, sock.makefile("rb") as reader:
        try:
            connection = _Connection(
                sock=sock,
                reader=reader,
                request_ids=itertools.count(1),
                deadline=time.monotonic() + time_limit_seconds,
            )
            _open_websocket(sock, reader)
            _call(
                connection,
                "initialize",
                {"clientInfo": {"name": _CLIENT_NAME, "version": "0"}, "capabilities": {"experimentalApi": True}},
            )
            initialized = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            _send_frame(sock, _OPCODE_TEXT, json.dumps(initialized).encode("utf-8"))
            yield connection
        except OSError as error:
            raise AppServerReadError(f"the app-server connection failed: {error}") from error
        with suppress(OSError):
            _send_frame(sock, _OPCODE_CLOSE, b"")


def read_codex_threads(socket_path: Path, time_limit_seconds: float) -> list[CodexThread]:
    """Every thread the app-server has loaded, with its status and background terminals."""
    with _initialized_connection(socket_path, time_limit_seconds) as connection:
        thread_ids = _list_all(connection, "thread/loaded/list", {})
        if not all(isinstance(thread_id, str) for thread_id in thread_ids):
            raise AppServerReadError("thread/loaded/list: an answer this writer does not know")
        return [_read_thread(connection, thread_id) for thread_id in thread_ids]


def plan_hook_trust(hooks_list_result: Any) -> dict[str, dict[str, str]]:
    """The ``hooks.state`` entries, by hook key, that trust each hook a ``hooks/list`` answer lists as new or changed."""
    entries = hooks_list_result.get("data") if isinstance(hooks_list_result, dict) else None
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("hooks"), list) for entry in entries
    ):
        raise AppServerReadError("hooks/list: an answer this script does not know")
    hooks = [hook for entry in entries for hook in entry["hooks"]]
    if not all(
        isinstance(hook, dict)
        and all(isinstance(hook.get(field), str) for field in ("key", "currentHash", "trustStatus"))
        for hook in hooks
    ):
        raise AppServerReadError("hooks/list: a hook this script does not know")
    return {
        hook["key"]: {"trusted_hash": hook["currentHash"]}
        for hook in hooks
        if hook["trustStatus"] in _HOOK_REVIEW_STATUSES
    }


def trust_hooks(socket_path: Path, cwd: Path, time_limit_seconds: float = DEFAULT_TIME_LIMIT_SECONDS) -> str:
    """Trust each hook the app-server on ``socket_path`` lists as new or changed in ``cwd``; return the outcome."""
    try:
        with _initialized_connection(socket_path, time_limit_seconds) as connection:
            trust_by_key = plan_hook_trust(_call(connection, "hooks/list", {"cwds": [str(cwd)]}))
            if trust_by_key:
                edit = {"keyPath": "hooks.state", "value": trust_by_key, "mergeStrategy": "upsert"}
                _call(connection, "config/batchWrite", {"edits": [edit], "reloadUserConfig": True})
    except (AppServerGoneError, AppServerReadError) as error:
        return f"unchanged: {error}"
    return f"{HOOKS_TRUSTED_OUTCOME_PREFIX} " + (" ".join(sorted(trust_by_key)) or "none")


def sync_records(
    state_dir: Path,
    socket_path: Path,
    now: int,
    ended_thread_id: str | None = None,
    time_limit_seconds: float = DEFAULT_TIME_LIMIT_SECONDS,
) -> str:
    """Rewrite this writer's records under ``state_dir`` from the app-server on ``socket_path``; return the outcome.

    One pass at a time holds the lock, so a pass never removes a record another wrote after it read codex.
    """
    record_dir = state_dir / RECORD_DIR_NAME
    lock_path = pass_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            threads = read_codex_threads(socket_path, time_limit_seconds)
        except AppServerGoneError as error:
            write_records(record_dir, {}, now)
            return f"cleared: {error}"
        except AppServerReadError as error:
            return f"unchanged: {error}"
        records = plan_records(threads, existing_record_names(record_dir), ended_thread_id)
        write_records(record_dir, records, now)
    return "recorded: " + (" ".join(sorted(records)) or "none")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--turn-ended", metavar="THREAD_ID", help="a thread whose turn is ending")
    action.add_argument("--trust-hooks", action="store_true", help="trust the hooks codex would ask to review")
    arguments = parser.parse_args()
    state_dir = Path(os.environ["MNGR_AGENT_STATE_DIR"])
    if arguments.trust_hooks:
        print(trust_hooks(app_server_socket_path(state_dir), Path.cwd()))
    else:
        print(sync_records(state_dir, app_server_socket_path(state_dir), int(time.time()), arguments.turn_ended))


if __name__ == "__main__":
    main()
