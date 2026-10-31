import inspect
import io
import os
import subprocess
import zipfile
from pathlib import Path

from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_archive
from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_command
from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.workspace_programs import backup_check
from imbue.minds.workspace_programs import backup_gate_probe
from imbue.minds.workspace_programs.backup_protocol import CHECK_RESULT_MARKER


def _archive_texts(archive: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        return {name: opened.read(name).decode("utf-8") for name in opened.namelist()}


def test_archive_holds_the_program_as_main_beside_the_package_modules_it_imports() -> None:
    texts = _archive_texts(build_workspace_program_archive(backup_gate_probe))

    assert sorted(texts) == [
        "__main__.py",
        "imbue/__init__.py",
        "imbue/minds/__init__.py",
        "imbue/minds/workspace_programs/__init__.py",
        "imbue/minds/workspace_programs/backup_protocol.py",
        "imbue/minds/workspace_programs/backup_support.py",
        "imbue/minds/workspace_programs/program_support.py",
    ]
    assert texts["__main__.py"] == inspect.getsource(backup_gate_probe)


def test_archive_bytes_depend_only_on_the_sources() -> None:
    # A workspace that keeps a copy of a program tells whether it is current by
    # comparing bytes, so nothing about the build (its time, above all) may leak in.
    archive = build_workspace_program_archive(backup_check)

    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        assert {entry.date_time for entry in opened.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_command_runs_the_program_from_a_temporary_file_with_its_arguments_and_removes_the_file(
    tmp_path: Path,
) -> None:
    # Outside any git repository the check cannot find the tag, which it then
    # reports as given: the argument with spaces arrived whole.
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    workspace = tmp_path / "not-a-repo"
    workspace.mkdir()
    command = build_workspace_program_command(
        backup_check, ("--minimum-tag", "minds-v9 with spaces", "--official-url", str(tmp_path / "nowhere"))
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=workspace,
        env={**os.environ, "TMPDIR": str(temp_dir), "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result
    payload = extract_marker_json(result.stdout, CHECK_RESULT_MARKER)
    assert payload is not None, result
    assert payload["target_tag"] == "minds-v9 with spaces"
    assert payload["code_state"] == "unverifiable"
    assert list(temp_dir.iterdir()) == []


def test_command_exits_with_the_program_status() -> None:
    # A program that dies before answering must not read as a clean exit.
    command = build_workspace_program_command(backup_gate_probe, ())
    python3_that_dies = "python3() { return 7; }; "

    result = subprocess.run(["bash", "-c", python3_that_dies + command], capture_output=True, text=True, timeout=60)

    assert result.returncode == 7, result


def test_extract_marker_json_finds_last_payload_amid_noise() -> None:
    stdout = 'warning: something\nMARKER:{"a": 1}\nnoise\nMARKER:{"a": 2}\ntrailing\n'
    assert extract_marker_json(stdout, "MARKER:") == {"a": 2}


def test_extract_marker_json_returns_none_without_marker() -> None:
    assert extract_marker_json("no marker here", "MARKER:") is None


def test_extract_marker_json_returns_none_for_bad_json() -> None:
    assert extract_marker_json("MARKER:{broken", "MARKER:") is None
