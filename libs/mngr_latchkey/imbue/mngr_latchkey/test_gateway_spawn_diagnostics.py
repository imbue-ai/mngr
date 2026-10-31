"""Integration test: a gateway that dies at startup leaves the evidence of why in the error and the log.

Lives here rather than in ``core_test.py`` because a failed spawn waits out the
gateway's full bind timeout.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError

# Present in the latchkey directory -> the fake gateway fails at startup.
_FAIL_MARKER = "gateway-fails-at-startup"
_STARTUP_EXIT_CODE = 3


@contextmanager
def _captured_log_records() -> Iterator[list[tuple[str, str]]]:
    """Collect every loguru record emitted inside the block as ``(level name, message)``."""
    captured: list[tuple[str, str]] = []
    sink_id = logger.add(lambda m: captured.append((m.record["level"].name, m.record["message"])), level=0)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _make_fake_latchkey_binary(tmp_path: Path) -> Path:
    """A ``latchkey`` whose gateway binds normally, or -- with the fail marker present -- prints and exits.

    The failing gateway echoes its listen password, so the test can check the
    reported output is redacted, and exits without ever listening.
    """
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, socket, sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    print('fake-jwt')\n"
        "    sys.exit(0)\n"
        'assert sys.argv[1] == "gateway"\n'
        f"if os.path.exists(os.path.join(os.environ['LATCHKEY_DIRECTORY'], '{_FAIL_MARKER}')):\n"
        "    print('starting with password ' + os.environ['LATCHKEY_GATEWAY_LISTEN_PASSWORD'], file=sys.stderr)\n"
        "    print('Error: synthetic startup failure', file=sys.stderr)\n"
        f"    sys.exit({_STARTUP_EXIT_CODE})\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((os.environ['LATCHKEY_GATEWAY_LISTEN_HOST'], int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


@pytest.mark.timeout(60)
def test_failed_gateway_respawn_reports_exit_code_output_and_port_state(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        original_port = manager.start_gateway(cg)
        password = manager.derive_gateway_password()
        running = manager._running_gateway
        assert running is not None
        running.process.terminate()
        (tmp_path / _FAIL_MARKER).touch()

        with _captured_log_records() as captured, pytest.raises(LatchkeyError) as exc_info:
            manager.start_gateway(cg)

        message = str(exc_info.value)
        assert f"did not bind 127.0.0.1:{original_port} within 10.0s" in message
        assert (
            f"(gateway exited with code {_STARTUP_EXIT_CODE} at the deadline; port free after cleanup; "
            "reused the previous gateway's port)" in message
        )
        output_warnings = [m for level, m in captured if level == "WARNING" and "Latchkey gateway output" in m]
        assert len(output_warnings) == 1
        assert "starting with password <redacted>" in output_warnings[0]
        assert "Error: synthetic startup failure" in output_warnings[0]
        assert password not in output_warnings[0]

        # The next attempt counts the failed one, and says when the gateway is back.
        # A failed spawn is never recorded, so the last running gateway it reports
        # is still the original one (which exited 0 on SIGTERM).
        (tmp_path / _FAIL_MARKER).unlink()
        with _captured_log_records() as captured:
            assert manager.start_gateway(cg) == original_port
        assert (
            "INFO",
            f"Respawning shared Latchkey gateway on its previous port 127.0.0.1:{original_port} "
            "(last running gateway exit code 0; 1 consecutive failed spawn(s) before this attempt)",
        ) in captured
        assert (
            "INFO",
            f"Shared Latchkey gateway came up on 127.0.0.1:{original_port} after 1 consecutive failed spawn(s)",
        ) in captured
        manager.stop_gateway()
