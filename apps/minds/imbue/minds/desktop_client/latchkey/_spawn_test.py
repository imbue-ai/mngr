import os
import signal
import socket
import threading
import time
from pathlib import Path

import psutil
import pytest

from imbue.minds.desktop_client.latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.minds.desktop_client.latchkey._spawn import spawn_detached_latchkey_gateway

_POLL_INTERVAL_SECONDS = 0.05


def _wait_for_file(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _make_ensure_browser_reporter_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` that records ``ensure-browser`` invocations and exits."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        'assert sys.argv[1] == "ensure-browser"\n'
        "report_path = os.environ['FAKE_LATCHKEY_REPORT']\n"
        "directory = os.environ.get('LATCHKEY_DIRECTORY', '')\n"
        "open(report_path, 'a').write(directory + '\\n')\n"
    )
    script.chmod(0o755)
    return script


def test_spawn_detached_latchkey_ensure_browser_invokes_subcommand_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_ensure_browser_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    monkeypatch.delenv("LATCHKEY_DIRECTORY", raising=False)
    log_path = tmp_path / "logs" / "ensure_browser.log"

    pid = spawn_detached_latchkey_ensure_browser(
        latchkey_binary=str(fake_binary),
        log_path=log_path,
    )
    assert pid > 0
    assert _wait_for_file(report_path)
    assert report_path.read_text() == "\n"
    # Log parent directory was created and the log file exists (child redirected stdio there).
    assert log_path.is_file()


def test_spawn_detached_latchkey_ensure_browser_sets_latchkey_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_ensure_browser_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    latchkey_directory = tmp_path / "shared_latchkey"
    assert not latchkey_directory.exists()

    pid = spawn_detached_latchkey_ensure_browser(
        latchkey_binary=str(fake_binary),
        log_path=tmp_path / "log",
        latchkey_directory=latchkey_directory,
    )
    assert pid > 0
    assert _wait_for_file(report_path)
    assert latchkey_directory.is_dir()
    assert report_path.read_text() == f"{latchkey_directory}\n"


def test_spawn_detached_latchkey_ensure_browser_raises_when_binary_missing(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here"
    with pytest.raises(FileNotFoundError):
        spawn_detached_latchkey_ensure_browser(
            latchkey_binary=str(missing),
            log_path=tmp_path / "log",
        )


def _make_fake_latchkey_binary(tmp_path: Path) -> Path:
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def _wait_for_listening(host: str, port: int, timeout: float = 5.0) -> bool:
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def test_spawn_detached_latchkey_gateway_binds_port_and_returns_pid(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    log_path = tmp_path / "logs" / "latchkey_gateway.log"
    # Allocate a free port up-front.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    pid = spawn_detached_latchkey_gateway(
        latchkey_binary=str(fake_binary),
        listen_host="127.0.0.1",
        listen_port=port,
        log_path=log_path,
    )
    try:
        assert pid > 0
        assert _wait_for_listening("127.0.0.1", port)
        # Log file should exist and the child should be running.
        assert log_path.is_file()
        assert psutil.pid_exists(pid)
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_spawn_detached_latchkey_gateway_raises_when_binary_missing(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here"
    with pytest.raises(FileNotFoundError):
        spawn_detached_latchkey_gateway(
            latchkey_binary=str(missing),
            listen_host="127.0.0.1",
            listen_port=65000,
            log_path=tmp_path / "log",
        )


def _make_env_reporter_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` that records ``LATCHKEY_DIRECTORY`` and blocks."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "directory = os.environ.get('LATCHKEY_DIRECTORY', '')\n"
        "report_path = os.environ['FAKE_LATCHKEY_REPORT']\n"
        "open(report_path, 'w').write(directory)\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def _make_permissions_env_reporter_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` that records ``LATCHKEY_PERMISSIONS_CONFIG`` and blocks."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "perms = os.environ.get('LATCHKEY_PERMISSIONS_CONFIG', '')\n"
        "report_path = os.environ['FAKE_LATCHKEY_REPORT']\n"
        "open(report_path, 'w').write(perms)\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def test_spawn_detached_latchkey_gateway_sets_latchkey_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_env_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    latchkey_directory = tmp_path / "shared_latchkey"
    assert not latchkey_directory.exists()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    pid = spawn_detached_latchkey_gateway(
        latchkey_binary=str(fake_binary),
        listen_host="127.0.0.1",
        listen_port=port,
        log_path=tmp_path / "log",
        latchkey_directory=latchkey_directory,
    )
    try:
        assert _wait_for_listening("127.0.0.1", port)
        # The shared directory was created up-front and passed through to the child.
        assert latchkey_directory.is_dir()
        assert report_path.read_text() == str(latchkey_directory)
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_spawn_detached_latchkey_gateway_without_directory_does_not_set_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_env_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    # Make sure the caller's own env var does not leak into the child.
    monkeypatch.delenv("LATCHKEY_DIRECTORY", raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    pid = spawn_detached_latchkey_gateway(
        latchkey_binary=str(fake_binary),
        listen_host="127.0.0.1",
        listen_port=port,
        log_path=tmp_path / "log",
    )
    try:
        assert _wait_for_listening("127.0.0.1", port)
        assert report_path.read_text() == ""
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_spawn_detached_latchkey_gateway_sets_permissions_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_permissions_env_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    monkeypatch.delenv("LATCHKEY_PERMISSIONS_CONFIG", raising=False)
    permissions_path = tmp_path / "agents" / "agent-x" / "latchkey_permissions.json"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    pid = spawn_detached_latchkey_gateway(
        latchkey_binary=str(fake_binary),
        listen_host="127.0.0.1",
        listen_port=port,
        log_path=tmp_path / "log",
        permissions_config_path=permissions_path,
    )
    try:
        assert _wait_for_listening("127.0.0.1", port)
        # The path is forwarded as-is even though the file does not exist yet.
        assert report_path.read_text() == str(permissions_path)
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_spawn_detached_latchkey_gateway_without_permissions_config_does_not_set_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_permissions_env_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    monkeypatch.delenv("LATCHKEY_PERMISSIONS_CONFIG", raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    pid = spawn_detached_latchkey_gateway(
        latchkey_binary=str(fake_binary),
        listen_host="127.0.0.1",
        listen_port=port,
        log_path=tmp_path / "log",
    )
    try:
        assert _wait_for_listening("127.0.0.1", port)
        assert report_path.read_text() == ""
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
