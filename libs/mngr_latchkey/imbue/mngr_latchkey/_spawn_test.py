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
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr_latchkey._spawn import _MAX_RAW_CAPTURE_BYTES
from imbue.mngr_latchkey._spawn import _MAX_RAW_CAPTURE_ROTATIONS
from imbue.mngr_latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.mngr_latchkey._spawn import spawn_detached_mngr_latchkey_forward
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import plugin_data_dir

_POLL_INTERVAL_SECONDS = 0.05

# Printed to stdout by the fake ``latchkey`` binary. The spawn helper hands the
# child's stdout fd straight to the capture file, so this is the child's own
# output landing in that file.
_CHILD_STDOUT_SENTINEL = "fake-latchkey-child-stdout"


def _wait_for_file_content(path: Path, timeout: float = 5.0) -> bool:
    """Wait until ``path`` exists *and* has been written to.

    Every caller reads the file's contents right after, and the detached child
    creates it before writing to it -- so waiting only for existence leaves a
    window where the read returns ''. Under parallel test load that window is
    wide enough to lose regularly.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_text_in_file(path: Path, expected: str, timeout: float = 5.0) -> bool:
    """Wait until ``path`` contains ``expected``.

    The child is detached, so its output reaches the capture file after the
    spawn call has already returned.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and expected in path.read_text():
            return True
        threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _make_ensure_browser_reporter_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` that records ``ensure-browser`` invocations and exits.

    It also prints :data:`_CHILD_STDOUT_SENTINEL`, so tests can observe where the
    child's own output lands in the raw capture file.
    """
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        'assert sys.argv[1] == "ensure-browser"\n'
        "report_path = os.environ['FAKE_LATCHKEY_REPORT']\n"
        "directory = os.environ.get('LATCHKEY_DIRECTORY', '')\n"
        "open(report_path, 'a').write(directory + '\\n')\n"
        f"print({_CHILD_STDOUT_SENTINEL!r}, flush=True)\n"
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
    assert _wait_for_file_content(report_path)
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
    assert _wait_for_file_content(report_path)
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
    assert _wait_for_file_content(report_path)
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
    assert _wait_for_file_content(report_path)
    assert report_path.read_text() == "operator-key\n"


def test_spawn_detached_latchkey_ensure_browser_raises_when_binary_missing(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here"
    with pytest.raises(FileNotFoundError):
        spawn_detached_latchkey_ensure_browser(
            latchkey_binary=str(missing),
            log_path=tmp_path / "log",
        )


def _make_argv_reporter_mngr_binary(tmp_path: Path) -> Path:
    """Build a fake ``mngr`` that records its argv to ``$FAKE_MNGR_REPORT`` and exits.

    Writes the report atomically (temp file + ``os.replace``) so a reader that
    polls only for the file's *existence* can never observe it truncated-but-empty
    between ``open(..., "w")`` and ``write`` -- the race that made
    :func:`test_spawn_detached_mngr_latchkey_forward_points_at_structured_log_file`
    intermittently raise ``JSONDecodeError`` under parallel load.
    """
    script = tmp_path / "mngr"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "path = os.environ['FAKE_MNGR_REPORT']\n"
        "tmp = path + '.tmp'\n"
        "open(tmp, 'w').write(json.dumps(sys.argv[1:]))\n"
        "os.replace(tmp, path)\n"
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
    assert _wait_for_file_content(report_path)
    argv = json.loads(report_path.read_text())
    # The forward process is pointed at its co-located structured JSONL log so
    # its timestamped events do not get mixed into the shared host-dir stream.
    assert "--log-file" in argv
    assert argv[argv.index("--log-file") + 1] == str(forward_events_log_path(plugin_dir))
    # ``--quiet`` suppresses the detached child's console handler so the raw
    # stdout/stderr capture file does not accumulate in steady state.
    assert "--quiet" in argv


def test_spawn_writes_timestamped_marker_above_child_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw capture opens with a marker dating the run that follows it.

    The child's own lines cannot be stamped (its fd is handed over directly), so
    the marker is the only thing tying a crash traceback in this file to a wall
    clock -- and thus to the timestamped logs uploaded alongside it. Checking it
    against the child's own output is what pins the relation that makes it mean
    anything: the marker has to be on disk before the descriptor is handed over,
    or it dates the wrong run.
    """
    fake_binary = _make_ensure_browser_reporter_binary(tmp_path)
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(tmp_path / "report"))
    log_path = tmp_path / "logs" / "ensure_browser.log"

    spawn_detached_latchkey_ensure_browser(latchkey_binary=str(fake_binary), log_path=log_path)

    assert _wait_for_text_in_file(log_path, _CHILD_STDOUT_SENTINEL)
    lines = log_path.read_text().splitlines()
    timestamp_text, _, description = lines[0].partition(" === ")
    assert description == "spawning latchkey ensure-browser ==="
    assert lines.index(_CHILD_STDOUT_SENTINEL) > 0
    # Parses as an aware UTC timestamp rather than merely "looking date-ish".
    parsed = datetime.fromisoformat(timestamp_text)
    assert parsed.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_spawn_rotates_an_oversized_raw_capture_and_prunes_old_rotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized capture is rotated at spawn, keeping the retention limit.

    Without this the file is append-only for the life of the install (its fd is
    handed to a detached child, so nothing truncates it) and is gzipped and
    re-uploaded with every bug report.
    """
    fake_binary = _make_ensure_browser_reporter_binary(tmp_path)
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(tmp_path / "report"))
    log_path = tmp_path / "logs" / "ensure_browser.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"x" * (_MAX_RAW_CAPTURE_BYTES + 1))
    # Pre-existing rotations, oldest first by name, to exercise pruning.
    for suffix in ("20260101000000000000", "20260102000000000000"):
        log_path.with_name(f"{log_path.name}.{suffix}").write_text("old")

    spawn_detached_latchkey_ensure_browser(latchkey_binary=str(fake_binary), log_path=log_path)

    rotation_names = sorted(path.name for path in log_path.parent.glob(f"{log_path.name}.*"))
    assert len(rotation_names) == _MAX_RAW_CAPTURE_ROTATIONS
    # Pruning keeps the newest, so the stale pre-seeded rotations are dropped and
    # what survives is the bulk just rotated out of the live file.
    assert "ensure_browser.log.20260101000000000000" not in rotation_names
    assert any(log_path.with_name(name).stat().st_size > _MAX_RAW_CAPTURE_BYTES for name in rotation_names)
    # The live file is fresh: the marker, not the rotated-away bulk.
    assert log_path.stat().st_size < _MAX_RAW_CAPTURE_BYTES


def test_spawn_leaves_a_small_raw_capture_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A capture under the cap is appended to, not rotated, so context survives."""
    fake_binary = _make_ensure_browser_reporter_binary(tmp_path)
    monkeypatch.setenv("FAKE_LATCHKEY_REPORT", str(tmp_path / "report"))
    log_path = tmp_path / "logs" / "ensure_browser.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("previous run output\n")

    spawn_detached_latchkey_ensure_browser(latchkey_binary=str(fake_binary), log_path=log_path)

    assert list(log_path.parent.glob(f"{log_path.name}.*")) == []
    assert "previous run output" in log_path.read_text()
