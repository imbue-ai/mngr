"""Unit tests for event_watcher.py."""

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_claude_zygote.conftest import write_changelings_settings_toml
from imbue.mng_claude_zygote.resources import event_watcher as event_watcher_module
from imbue.mng_claude_zygote.resources.event_watcher import _check_all_sources
from imbue.mng_claude_zygote.resources.event_watcher import _check_and_send_new_events
from imbue.mng_claude_zygote.resources.event_watcher import _get_offset
from imbue.mng_claude_zygote.resources.event_watcher import _load_watcher_settings
from imbue.mng_claude_zygote.resources.event_watcher import _set_offset


class SubprocessCapture:
    """Records calls to subprocess.run for assertion in tests."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._returncode = returncode
        self._stderr = stderr

    def run(self, cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        self.calls.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=self._returncode, stdout="", stderr=self._stderr)


@pytest.fixture()
def mock_subprocess_success(monkeypatch: pytest.MonkeyPatch) -> SubprocessCapture:
    """Replace event_watcher's subprocess with a recording stub (returncode=0)."""
    capture = SubprocessCapture(returncode=0)
    mock_sp = types.SimpleNamespace(run=capture.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    return capture


@pytest.fixture()
def mock_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> SubprocessCapture:
    """Replace event_watcher's subprocess with a recording stub (returncode=1)."""
    capture = SubprocessCapture(returncode=1, stderr="send failed")
    mock_sp = types.SimpleNamespace(run=capture.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    return capture


# -- _load_watcher_settings tests --


def test_load_settings_defaults_when_no_file(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 3
    assert settings.sources == ["messages", "scheduled", "mng_agents", "stop"]


def test_load_settings_reads_from_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(
        tmp_path, '[watchers]\nevent_poll_interval_seconds = 10\nwatched_event_sources = ["messages", "stop"]\n'
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 10
    assert settings.sources == ["messages", "stop"]


def test_load_settings_handles_corrupt_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "this is not valid toml {{{")
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 3


def test_load_settings_handles_partial_config(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "[watchers]\nevent_poll_interval_seconds = 7\n")
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 7
    assert settings.sources == ["messages", "scheduled", "mng_agents", "stop"]


# -- _get_offset / _set_offset tests --


def test_get_offset_returns_zero_when_missing(tmp_path: Path) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    assert _get_offset(offsets_dir, "messages") == 0


def test_set_and_get_offset_roundtrip(tmp_path: Path) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    _set_offset(offsets_dir, "messages", 42)
    assert _get_offset(offsets_dir, "messages") == 42


def test_get_offset_returns_zero_for_corrupt_file(tmp_path: Path) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    (offsets_dir / "messages.offset").write_text("not_a_number")
    assert _get_offset(offsets_dir, "messages") == 0


def test_set_offset_overwrites_previous(tmp_path: Path) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    _set_offset(offsets_dir, "messages", 10)
    _set_offset(offsets_dir, "messages", 20)
    assert _get_offset(offsets_dir, "messages") == 20


# -- _check_and_send_new_events tests --


def test_check_and_send_does_nothing_when_no_events_file(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    """No crash when events file does not exist."""
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()

    _check_and_send_new_events(tmp_path / "events.jsonl", "test_source", offsets_dir, "agent")
    assert len(mock_subprocess_success.calls) == 0


def test_check_and_send_does_nothing_when_at_current_offset(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n')
    _set_offset(offsets_dir, "test_source", 1)

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "agent")
    assert len(mock_subprocess_success.calls) == 0


def test_check_and_send_sends_new_events_and_updates_offset(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n{"event": 2}\n{"event": 3}\n')
    (offsets_dir / "test_source.offset").write_text("1")

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "my-agent")

    assert len(mock_subprocess_success.calls) == 1
    cmd = mock_subprocess_success.calls[0][0]
    assert "mng" in cmd and "message" in cmd
    assert "my-agent" in cmd
    assert '{"event": 2}' in cmd[-1]
    assert '{"event": 3}' in cmd[-1]
    assert _get_offset(offsets_dir, "test_source") == 3


def test_check_and_send_does_not_update_offset_on_failure(
    tmp_path: Path,
    mock_subprocess_failure: SubprocessCapture,
) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n{"event": 2}\n')

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "my-agent")
    assert _get_offset(offsets_dir, "test_source") == 0


def test_check_and_send_handles_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n')

    def timeout_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    mock_sp = types.SimpleNamespace(run=timeout_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "agent")
    assert _get_offset(offsets_dir, "test_source") == 0


def test_check_and_send_handles_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n')

    def os_error_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise OSError("subprocess launch failed")

    mock_sp = types.SimpleNamespace(run=os_error_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "agent")
    assert _get_offset(offsets_dir, "test_source") == 0


def test_check_and_send_skips_empty_new_lines(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    """When new lines are all whitespace, should not send a message."""
    offsets_dir = tmp_path / "offsets"
    offsets_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text('{"event": 1}\n\n\n')
    _set_offset(offsets_dir, "test_source", 1)

    _check_and_send_new_events(events_file, "test_source", offsets_dir, "agent")
    assert len(mock_subprocess_success.calls) == 0


# -- _check_all_sources tests --


def test_check_all_sources_iterates_all_sources(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    logs_dir = tmp_path / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True)

    for source in ("messages", "stop"):
        source_dir = logs_dir / source
        source_dir.mkdir(parents=True)
        (source_dir / "events.jsonl").write_text(f'{{"source": "{source}"}}\n')

    _check_all_sources(logs_dir, ["messages", "stop"], offsets_dir, "agent")
    assert len(mock_subprocess_success.calls) == 2


def test_check_all_sources_skips_missing_event_files(
    tmp_path: Path,
    mock_subprocess_success: SubprocessCapture,
) -> None:
    logs_dir = tmp_path / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True)

    _check_all_sources(logs_dir, ["nonexistent"], offsets_dir, "agent")
    assert len(mock_subprocess_success.calls) == 0
