"""The workspace update program's command modes, run in-process against a staged workspace."""

import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from inline_snapshot import snapshot

from imbue.minds.desktop_client.testing import STAGED_CREATED_CHAT
from imbue.minds.desktop_client.testing import ScriptedToolAnswer
from imbue.minds.desktop_client.testing import StagedUpdateWorkspace
from imbue.minds.desktop_client.testing import load_update_workspace_program
from imbue.minds.desktop_client.testing import read_update_program_answer
from imbue.minds.desktop_client.testing import stage_update_workspace
from imbue.minds.desktop_client.testing import update_program_context
from imbue.minds.desktop_client.update_status import UpdateArmOutcome
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceSettings
from imbue.minds.desktop_client.update_workspace_command import build_arm_arguments
from imbue.minds.desktop_client.update_workspace_command import build_now_arguments
from imbue.minds.desktop_client.update_workspace_command import build_read_arguments
from imbue.minds.desktop_client.update_workspace_command import build_update_workspace_command
from imbue.minds.desktop_client.update_workspace_command import parse_update_command_result
from imbue.minds.desktop_client.update_workspace_command import parse_update_workspace_facts

_NOW = 1_792_000_000.0
_SETTINGS = UpdateWorkspaceSettings(
    ceiling_ref="minds-v0.9.0",
    window_start_hour=2,
    window_end_hour=5,
    zone="America/Los_Angeles",
    is_backup_configured=True,
)


def _run_mode(
    program: ModuleType,
    staged: StagedUpdateWorkspace,
    mode: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    program.MODES[mode](update_program_context(program, staged, argv, _NOW))
    return read_update_program_answer(capsys.readouterr().out)


def test_arming_installs_the_dispatcher_and_cron_line_and_writes_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, "minds-v0.9.1"), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.ARMED
    assert result.schedule is not None
    assert result.schedule.target_ref == "minds-v0.9.1"
    assert (result.schedule.window_start_hour, result.schedule.window_end_hour, result.schedule.zone) == (
        2,
        5,
        "America/Los_Angeles",
    )
    assert staged.read_state("schedule.json") == answer["schedule"]
    dispatcher = staged.root / "data" / ".state" / "update-apply" / "dispatcher.py"
    assert dispatcher.read_text() == Path(program.__file__ or "").read_text()
    live_line = (staged.live_cron_dir / "update-self-dispatch").read_text()
    assert live_line == (staged.root / "data" / ".state" / "cron.d" / "update-self-dispatch").read_text()
    assert "flock -n /run/update-self-dispatch.lock" in live_line
    assert f"cd {staged.root.resolve()} && python3 data/.state/update-apply/dispatcher.py tick" in live_line


@pytest.mark.parametrize(
    ("has_skill", "is_signed_in", "has_cron", "outcome"),
    [
        (True, True, False, UpdateArmOutcome.NO_CRON),
        (False, True, True, UpdateArmOutcome.TOO_OLD_TO_UPDATE),
        (True, False, True, UpdateArmOutcome.NEEDS_SIGN_IN),
    ],
)
@pytest.mark.witnesses("workspace-updates.no-cron", partial="the refusal; not the app's Update now offer")
def test_arming_refuses_a_workspace_that_cannot_run_a_scheduled_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_skill: bool,
    is_signed_in: bool,
    has_cron: bool,
    outcome: UpdateArmOutcome,
) -> None:
    staged = stage_update_workspace(
        tmp_path, monkeypatch, has_skill=has_skill, is_signed_in=is_signed_in, has_cron=has_cron
    )
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is outcome
    assert staged.read_state("schedule.json") is None


def test_re_arming_keeps_the_chat_the_dispatcher_already_started_this_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second arm inside the window must not let the dispatcher start a second run."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)
    _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, ""), capsys)
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    staged.write_state("schedule.json", {**schedule, "marked_chat": "update-to-minds-v0-9-0", "marked_at": _NOW - 60})

    _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, ""), capsys)

    rearmed = staged.read_state("schedule.json")
    assert rearmed is not None
    assert (rearmed["marked_chat"], rearmed["marked_at"]) == ("update-to-minds-v0-9-0", _NOW - 60)


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _labels(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == "--label"]


@pytest.mark.witnesses("workspace-updates.one-seed-message")
def test_update_now_creates_the_chat_through_the_templates_messenger_with_the_pinned_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.CREATED
    assert (result.chat_agent_name, result.chat_id) == (STAGED_CREATED_CHAT["name"], STAGED_CREATED_CHAT["chat_id"])
    creates = staged.chat_creates()
    assert len(creates) == 1
    assert (_flag(creates[0], "--name"), _flag(creates[0], "--title")) == (
        "update-to-minds-v0-9-0",
        "Update to v0.9.0",
    )
    assert _labels(creates[0]) == ["assist=true", "auto_open=true"]
    assert creates[0][-1] == snapshot("""\
/update-self

The Imbue Studio app pinned this update, so do not ask it for its version:
- Target: minds-v0.9.0
- Ceiling: minds-v0.9.0
When you resolve the target in Step 2, append `--ceiling minds-v0.9.0` to the `resolve-target` command. If the app cannot be reached for anything else during this run, that is not a fault.\
""")
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["chat_agent_name"], dispatch["started_by"], dispatch["ceiling"]) == (
        "update-to-minds-v0-9-0",
        "immediate",
        "minds-v0.9.0",
    )


def test_an_override_without_backups_carries_both_notes_and_names_the_chat_for_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)
    settings = UpdateWorkspaceSettings(
        ceiling_ref="minds-v0.9.0",
        window_start_hour=2,
        window_end_hour=5,
        zone="America/Los_Angeles",
        is_backup_configured=False,
    )

    _run_mode(program, staged, "now", build_now_arguments(settings, "upstream/main"), capsys)

    create = staged.chat_creates()[0]
    assert (_flag(create, "--name"), _flag(create, "--title")) == (
        "update-to-upstream-main",
        "Update to upstream/main",
    )
    paragraphs = create[-1].split("\n\n")
    assert paragraphs[1].startswith("The Imbue Studio app pinned this update")
    assert "- Target: upstream/main\n- Ceiling: minds-v0.9.0" in paragraphs[1]
    assert paragraphs[2].startswith(
        "The user chose a specific version in the Imbue Studio app: update this workspace to upstream/main."
    )
    assert paragraphs[3].startswith("This workspace has no backups configured")


def test_a_messenger_from_before_the_title_is_asked_again_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(exit_code=2, stderr="message_chat.py: error: unrecognized arguments: --title\n"),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is UpdateArmOutcome.CREATED
    assert ["--title" in create for create in staged.chat_creates()] == [True, False]


def test_a_taken_chat_name_gets_the_next_free_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Earlier runs' chats stay as history, so a second update to the same release needs its own name."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.set_agents([{"name": "update-to-minds-v0-9-0", "id": "agent-" + "b" * 32, "state": "WAITING"}])
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="The chat app did not create the chat: "
                "A chat named 'update-to-minds-v0-9-0-2' already exists; pick another name\n",
            ),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )
    program = load_update_workspace_program(tmp_path)

    _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert [_flag(create, "--name") for create in staged.chat_creates()] == [
        "update-to-minds-v0-9-0-2",
        "update-to-minds-v0-9-0-3",
    ]


def test_update_now_adopts_a_run_already_under_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {"chat_agent_name": "update-to-minds-v0-8-7", "started_at": _NOW - 600})
    staged.set_agents([{"name": "update-to-minds-v0-8-7", "id": "agent-" + "d" * 32, "state": "RUNNING"}])
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.ALREADY_RUNNING
    assert (result.chat_agent_name, result.chat_id) == ("update-to-minds-v0-8-7", "agent-" + "d" * 32)
    assert staged.chat_creates() == []


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        (
            "The chat app did not create the chat: Connect an AI provider to start chatting.\n",
            UpdateArmOutcome.NEEDS_SIGN_IN,
        ),
        ("The chat app did not create the chat: mngr create exited 1\n", UpdateArmOutcome.CREATE_FAILED),
        (
            "Falling back to `mngr create`: could not connect to the chat app\nmngr create failed\n",
            UpdateArmOutcome.CHAT_APP_UNREACHABLE,
        ),
    ],
)
def test_a_create_the_chat_app_refused_reads_as_the_refusal_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
    outcome: UpdateArmOutcome,
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.script_tool("system/scripts/message_chat.py", [ScriptedToolAnswer(exit_code=1, stderr=stderr)])
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is outcome
    assert result.detail == stderr.strip()
    assert staged.read_state("last-dispatch.json") is None


def test_a_workspace_with_no_messenger_gets_a_bare_create_carrying_the_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_message_chat=False)
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is UpdateArmOutcome.CREATED
    creates = [call for call in staged.recorded_calls() if call[:1] == ["create"]]
    assert len(creates) == 1
    assert creates[0][1] == "update-to-minds-v0-9-0"
    assert "display_name=Update to v0.9.0" in creates[0]
    assert "auto_open=true" in creates[0]


@pytest.mark.witnesses("workspace-updates.schedule-follows-the-app")
def test_reading_an_armed_schedule_refreshes_the_apps_fields_and_keeps_the_dispatchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)
    _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, "minds-v0.9.1"), capsys)
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    staged.write_state(
        "schedule.json",
        {**schedule, "marked_chat": "update-to-minds-v0-9-1", "marked_at": _NOW, "last_skip_reason": "LEASE_HELD"},
    )
    staged.write_state("run.json", {"chat_agent_name": "update-to-minds-v0-9-1", "started_at": _NOW})
    moved = UpdateWorkspaceSettings(
        ceiling_ref="minds-v0.10.0",
        window_start_hour=23,
        window_end_hour=3,
        zone="Europe/Berlin",
        is_backup_configured=False,
    )

    answer = _run_mode(program, staged, "read", build_read_arguments(moved, is_sign_in_checked=False), capsys)

    facts = parse_update_workspace_facts(answer)
    assert facts.schedule is not None
    assert (facts.schedule.ceiling_ref, facts.schedule.window_start_hour, facts.schedule.window_end_hour) == (
        "minds-v0.10.0",
        23,
        3,
    )
    assert facts.schedule.zone == "Europe/Berlin"
    assert facts.schedule.override_ref == "minds-v0.9.1"
    assert facts.schedule.marked_chat_name == "update-to-minds-v0-9-1"
    assert facts.schedule.last_skip_reason == "LEASE_HELD"
    assert facts.run_status is not None and facts.run_status.chat_agent_name == "update-to-minds-v0-9-1"


def test_reading_a_workspace_with_nothing_armed_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)

    answer = _run_mode(program, staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    facts = parse_update_workspace_facts(answer)
    assert (facts.schedule, facts.run_status, facts.last_dispatch) == (None, None, None)
    assert not (staged.root / "data" / ".state" / "update-apply").exists()


@pytest.mark.parametrize("is_signed_in", [True, False])
def test_a_read_asked_about_the_account_says_whether_one_is_signed_in_with_nothing_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], is_signed_in: bool
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=is_signed_in)
    program = load_update_workspace_program(tmp_path)

    asked = _run_mode(program, staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=True), capsys)
    unasked = _run_mode(program, staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    assert parse_update_workspace_facts(asked).is_signed_in is is_signed_in
    assert parse_update_workspace_facts(unasked).is_signed_in is None


def test_cancelling_deletes_the_schedule_and_says_whether_one_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    program = load_update_workspace_program(tmp_path)
    _run_mode(program, staged, "arm", build_arm_arguments(_SETTINGS, ""), capsys)

    first = _run_mode(program, staged, "cancel", [], capsys)
    second = _run_mode(program, staged, "cancel", [], capsys)

    assert (first["is_cancelled"], second["is_cancelled"]) == (True, False)
    assert staged.read_state("schedule.json") is None


def test_the_shipped_command_runs_the_program_from_a_scratch_file_through_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What ``mngr exec`` runs: decode, run in the workspace root, remove the scratch file, keep the exit status."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", {"window_start_hour": 2, "window_end_hour": 5})
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch_dir))

    completed = subprocess.run(
        ["bash", "-c", build_update_workspace_command("cancel", [])],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=staged.root,
    )

    assert completed.returncode == 0, completed.stderr
    assert read_update_program_answer(completed.stdout)["is_cancelled"] is True
    assert staged.read_state("schedule.json") is None
    assert list(scratch_dir.iterdir()) == []
