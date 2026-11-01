"""Unit tests for the in-workspace backup-verification flag program."""

from pathlib import Path

import pytest

from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.workspace_programs import backup_verification_flag
from imbue.minds.workspace_programs.backup_protocol import VERIFICATION_FLAG_RESULT_MARKER
from imbue.minds.workspace_programs.backup_verification_flag import read_verification_disabled
from imbue.minds.workspace_programs.backup_verification_flag import render_backup_toml_with_flag
from imbue.minds.workspace_programs.backup_verification_flag import write_verification_disabled


def test_the_flag_defaults_to_enabled_for_a_missing_or_unreadable_file(tmp_path: Path) -> None:
    absent = str(tmp_path / "backup.toml")
    assert read_verification_disabled(absent) is False

    (tmp_path / "broken.toml").write_text("not = toml =")
    assert read_verification_disabled(str(tmp_path / "broken.toml")) is False


def test_setting_the_flag_keeps_every_other_line_of_the_users_config(tmp_path: Path) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(
        '# my settings\nbackup_interval_seconds = 120\nextra_excludes = ["**/scratch"]\n[retention]\nkeep_hourly = 4\n'
    )

    write_verification_disabled(True, str(path))

    text = path.read_text()
    assert text.startswith("verification_disabled = true\n")
    assert "# my settings" in text
    assert "backup_interval_seconds = 120" in text
    assert "keep_hourly = 4" in text
    assert read_verification_disabled(str(path)) is True


def test_setting_the_flag_replaces_an_existing_root_line_rather_than_adding_another(tmp_path: Path) -> None:
    path = tmp_path / "backup.toml"
    path.write_text("verification_disabled = true\n[retention]\nkeep_hourly = 4\n")

    write_verification_disabled(False, str(path))

    text = path.read_text()
    assert text.count("verification_disabled") == 1
    assert read_verification_disabled(str(path)) is False


def test_setting_the_flag_creates_the_file_when_the_workspace_has_no_backup_config(tmp_path: Path) -> None:
    path = tmp_path / "data" / "system" / "backup.toml"

    write_verification_disabled(True, str(path))

    assert read_verification_disabled(str(path)) is True


def test_a_key_inside_a_table_with_the_same_name_is_left_alone() -> None:
    rendered = render_backup_toml_with_flag("[snapshot]\nverification_disabled = true\n", False)

    assert rendered.startswith("verification_disabled = false\n")
    assert "[snapshot]\nverification_disabled = true" in rendered


def test_the_program_answers_the_flag_and_whether_a_set_was_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    backup_verification_flag.main(["--set", "disabled"])
    answer = extract_marker_json(capsys.readouterr().out, VERIFICATION_FLAG_RESULT_MARKER)
    assert answer == {"schema": 1, "verification_disabled": True, "is_set_applied": True}

    backup_verification_flag.main([])
    answer = extract_marker_json(capsys.readouterr().out, VERIFICATION_FLAG_RESULT_MARKER)
    assert answer == {"schema": 1, "verification_disabled": True, "is_set_applied": False}
