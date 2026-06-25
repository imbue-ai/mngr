"""Tests for the remaining detached-spawn helpers in :mod:`_spawn`.

Only ``spawn_detached_latchkey_ensure_browser`` lives here now: the
shared ``latchkey gateway`` subprocess moved to ``ConcurrencyGroup``-based
spawning in ``core.py`` (env wiring + binary-missing handling are
exercised by the integration tests in ``core_test.py``), and
``spawn_detached_mngr_latchkey_forward`` is exercised through
``forward_supervisor_test.py``.
"""

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr_latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.mngr_latchkey._spawn import spawn_detached_mngr_latchkey_forward
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import plugin_data_dir

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


@pytest.mark.flaky
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


def _make_encryption_key_reporter_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` that records ``LATCHKEY_ENCRYPTION_KEY`` and exits."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        'assert sys.argv[1] == "ensure-browser"\n'
        "report_path = os.environ['FAKE_LATCHKEY_REPORT']\n"
        "key = os.environ.get('LATCHKEY_ENCRYPTION_KEY', '')\n"
        "open(report_path, 'a').write(key + '\\n')\n"
    )
    script.chmod(0o755)
    return script


def test_spawn_detached_latchkey_ensure_browser_injects_encryption_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_encryption_key_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    monkeypatch.delenv("LATCHKEY_ENCRYPTION_KEY", raising=False)

    pid = spawn_detached_latchkey_ensure_browser(
        latchkey_binary=str(fake_binary),
        log_path=tmp_path / "log",
        encryption_key=SecretStr("per-directory-key"),
    )
    assert pid > 0
    assert _wait_for_file(report_path)
    # The child sees the per-directory key, so Latchkey never falls through to
    # the system keychain (which on macOS would pop an access dialog).
    assert report_path.read_text() == "per-directory-key\n"


def test_spawn_detached_latchkey_ensure_browser_operator_key_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_encryption_key_reporter_binary(tmp_path)
    report_path = tmp_path / "report"
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(report_path))
    # An operator-set value already in the environment must win over the
    # per-directory key passed by the caller.
    monkeypatch.setenv("LATCHKEY_ENCRYPTION_KEY", "operator-key")

    pid = spawn_detached_latchkey_ensure_browser(
        latchkey_binary=str(fake_binary),
        log_path=tmp_path / "log",
        encryption_key=SecretStr("per-directory-key"),
    )
    assert pid > 0
    assert _wait_for_file(report_path)
    assert report_path.read_text() == "operator-key\n"


def test_spawn_detached_latchkey_ensure_browser_raises_when_binary_missing(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here"
    with pytest.raises(FileNotFoundError):
        spawn_detached_latchkey_ensure_browser(
            latchkey_binary=str(missing),
            log_path=tmp_path / "log",
        )


def _make_argv_reporter_mngr_binary(tmp_path: Path) -> Path:
    """Build a fake ``mngr`` that records its argv to ``$FAKE_MNGR_REPORT`` and exits."""
    script = tmp_path / "mngr"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['FAKE_MNGR_REPORT'], 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    script.chmod(0o755)
    return script


def test_spawn_detached_mngr_latchkey_forward_points_at_structured_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary = _make_argv_reporter_mngr_binary(tmp_path)
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("FAKE_MNGR_REPORT", str(report_path))
    latchkey_directory = tmp_path / "latchkey"
    plugin_dir = plugin_data_dir(latchkey_directory)

    pid = spawn_detached_mngr_latchkey_forward(
        mngr_binary=str(fake_binary),
        latchkey_binary="latchkey",
        latchkey_directory=latchkey_directory,
        log_path=forward_log_path(plugin_dir),
    )
    assert pid > 0
    assert _wait_for_file(report_path)
    argv = json.loads(report_path.read_text())
    # The forward process is pointed at its co-located structured JSONL log so
    # its timestamped events do not get mixed into the shared host-dir stream.
    assert "--log-file" in argv
    assert argv[argv.index("--log-file") + 1] == str(forward_events_log_path(plugin_dir))
    # ``--quiet`` suppresses the detached child's console handler so the raw
    # stdout/stderr capture file does not accumulate in steady state.
    assert "--quiet" in argv
