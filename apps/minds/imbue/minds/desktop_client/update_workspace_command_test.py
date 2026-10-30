"""The workspace update program's command modes, run in-process against a staged workspace."""

import json
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from inline_snapshot import snapshot

from imbue.minds.desktop_client.testing import STAGED_CREATED_CHAT
from imbue.minds.desktop_client.testing import ScriptedToolAnswer
from imbue.minds.desktop_client.testing import StagedUpdateWorkspace
from imbue.minds.desktop_client.testing import armed_update_schedule_file
from imbue.minds.desktop_client.testing import read_update_program_answer
from imbue.minds.desktop_client.testing import stage_update_workspace
from imbue.minds.desktop_client.testing import update_program_context
from imbue.minds.desktop_client.update_status import UpdateArmOutcome
from imbue.minds.desktop_client.update_status import UpdateSkipReason
from imbue.minds.desktop_client.update_status import UpdateStartedBy
from imbue.minds.desktop_client.update_status import parse_update_schedule_record
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceSettings
from imbue.minds.desktop_client.update_workspace_command import UtcUpdateWindow
from imbue.minds.desktop_client.update_workspace_command import build_arm_arguments
from imbue.minds.desktop_client.update_workspace_command import build_now_arguments
from imbue.minds.desktop_client.update_workspace_command import build_read_arguments
from imbue.minds.desktop_client.update_workspace_command import build_update_workspace_command
from imbue.minds.desktop_client.update_workspace_command import parse_update_command_result
from imbue.minds.desktop_client.update_workspace_command import parse_update_workspace_facts
from imbue.minds.desktop_client.update_workspace_command import utc_update_window
from imbue.minds.workspace_programs import update_protocol
from imbue.minds.workspace_programs import update_workspace
from imbue.minds.workspace_programs.update_dispatcher import DISPATCHER_ARCHIVE_PATH

_NOW = 1_792_000_000.0
_WINDOW = UtcUpdateWindow(start_minute_utc=9 * 60, length_minutes=3 * 60)
_SETTINGS = UpdateWorkspaceSettings(ceiling_ref="minds-v0.9.0", window=_WINDOW, is_backup_configured=True)


@pytest.mark.parametrize(
    ("now", "window_hours", "zone", "start_minute_utc", "length_minutes"),
    [
        # Inside a window: the window open now.
        ("2026-10-20T03:00:00-07:00", (2, 5), "America/Los_Angeles", 9 * 60, 3 * 60),
        ("2026-10-20T08:00:00+02:00", (11, 12), "Europe/Berlin", 9 * 60, 60),
        # A half-hour zone opens off the UTC hour.
        ("2026-10-20T12:00:00+05:30", (2, 5), "Asia/Kolkata", 20 * 60 + 30, 3 * 60),
        # A window that wraps local midnight, read after midnight, is the one that opened the evening before.
        ("2026-10-21T01:00:00-07:00", (23, 3), "America/Los_Angeles", 6 * 60, 4 * 60),
        # Outside every window: the next one, at its own offset (here past the fall-back on 2026-11-01).
        ("2026-10-31T12:00:00-07:00", (2, 5), "America/Los_Angeles", 10 * 60, 3 * 60),
    ],
)
@pytest.mark.witnesses("workspace-updates.window-in-the-users-zone")
def test_the_users_local_window_reaches_the_workspace_in_utc(
    now: str, window_hours: tuple[int, int], zone: str, start_minute_utc: int, length_minutes: int
) -> None:
    window = utc_update_window(window_hours[0], window_hours[1], ZoneInfo(zone), datetime.fromisoformat(now))

    assert (window.start_minute_utc, window.length_minutes) == (start_minute_utc, length_minutes)


def _run_mode(
    staged: StagedUpdateWorkspace,
    mode: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    update_workspace.MODES[mode](update_program_context(staged, argv, _NOW))
    return read_update_program_answer(capsys.readouterr().out)


def test_arming_installs_the_dispatcher_and_cron_line_and_writes_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)

    answer = _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "minds-v0.9.1", is_auto_armed=False), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.ARMED
    assert result.schedule is not None
    assert result.schedule.target_ref == "minds-v0.9.1"
    assert (result.schedule.window_start_minute_utc, result.schedule.window_length_minutes) == (9 * 60, 3 * 60)
    assert staged.read_state("schedule.json") == answer["schedule"]
    dispatcher = staged.root / "data" / ".state" / "update-apply" / "dispatcher.pyz"
    assert dispatcher.read_bytes() == staged.program_archive.read_bytes()
    live_line = (staged.live_cron_dir / "update-self-dispatch").read_text()
    assert live_line == (staged.root / "data" / ".state" / "cron.d" / "update-self-dispatch").read_text()
    assert "flock -n /run/update-self-dispatch.lock" in live_line
    assert f"cd {staged.root.resolve()} && python3 data/.state/update-apply/dispatcher.pyz tick" in live_line


@pytest.mark.parametrize("is_auto_armed", (True, False))
def test_the_schedule_file_records_whether_auto_update_armed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], is_auto_armed: bool
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)

    answer = _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=is_auto_armed), capsys)

    schedule = parse_update_command_result(answer).schedule
    assert schedule is not None
    assert schedule.is_auto_armed is is_auto_armed
    assert staged.read_state("schedule.json") == answer["schedule"]


def test_a_schedule_file_that_does_not_say_who_armed_it_reads_as_the_users() -> None:
    """Every file written before auto-update existed was armed by a press."""
    legacy = armed_update_schedule_file(ceiling_ref="minds-v0.9.0")
    del legacy["is_auto_armed"]

    schedule = parse_update_schedule_record(legacy)

    assert schedule is not None
    assert schedule.is_auto_armed is False


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

    answer = _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=False), capsys)

    assert parse_update_command_result(answer).outcome is outcome
    assert staged.read_state("schedule.json") is None


def test_re_arming_keeps_the_chat_the_dispatcher_already_started_this_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second arm inside the window must not let the dispatcher start a second run."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=False), capsys)
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    staged.write_state("schedule.json", {**schedule, "marked_chat": "update-to-minds-v0-9-0", "marked_at": _NOW - 60})

    _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=False), capsys)

    rearmed = staged.read_state("schedule.json")
    assert rearmed is not None
    assert (rearmed["marked_chat"], rearmed["marked_at"]) == ("update-to-minds-v0-9-0", _NOW - 60)


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _labels(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == "--label"]


def test_a_mode_the_app_runs_keeps_its_warnings_in_the_dispatcher_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app drops the stderr of a mode that answered, so a warning lives only if the log has it."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.set_agents([], is_services_agent_listed=False)

    _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    log_lines = (staged.root / "data" / ".state" / "update-apply" / "dispatcher.log").read_text().splitlines()
    assert any(
        line.endswith("mngr list did not list this workspace's services agent; ignoring it") for line in log_lines
    )


@pytest.mark.witnesses("workspace-updates.one-seed-message")
def test_update_now_creates_the_chat_through_the_templates_messenger_with_the_pinned_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.CREATED
    assert (result.chat_agent_name, result.chat_id) == (STAGED_CREATED_CHAT["name"], STAGED_CREATED_CHAT["chat_id"])
    creates = staged.chat_creates()
    assert len(creates) == 1
    assert _flag(creates[0], "--name") == "Update to v0.9.0"
    labels = _labels(creates[0])
    assert labels[:2] == ["assist=true", "auto_open=true"]
    attempt = staged.read_state("dispatch-attempt.json")
    assert attempt is not None and labels[2:] == [f"update_dispatch_attempt={attempt['attempt_id']}"]
    assert creates[0][-1] == snapshot("""\
/update-self

The Imbue Studio app pinned this update, so do not ask it for its version:
- Target: minds-v0.9.0
- Ceiling: minds-v0.9.0
If Step 1 finds uncommitted changes, do not stop the update for them: commit them all as one work-in-progress commit on the current branch, with a message that lists their paths, name that commit in your results, and carry on.
When you resolve the target in Step 2, append `--ceiling minds-v0.9.0` to the `resolve-target` command. If the app cannot be reached for anything else during this run, that is not a fault.\
""")
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["chat_agent_name"], dispatch["started_by"], dispatch["ceiling"]) == (
        STAGED_CREATED_CHAT["name"],
        "immediate",
        "minds-v0.9.0",
    )


def test_an_override_without_backups_carries_both_notes_and_names_the_chat_for_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    settings = UpdateWorkspaceSettings(ceiling_ref="minds-v0.9.0", window=_WINDOW, is_backup_configured=False)

    _run_mode(staged, "now", build_now_arguments(settings, "upstream/main"), capsys)

    create = staged.chat_creates()[0]
    assert _flag(create, "--name") == "Update to upstream/main"
    paragraphs = create[-1].split("\n\n")
    assert paragraphs[1].startswith("The Imbue Studio app pinned this update")
    assert "- Target: upstream/main\n- Ceiling: minds-v0.9.0" in paragraphs[1]
    assert paragraphs[2].startswith(
        "The user chose a specific version in the Imbue Studio app: update this workspace to upstream/main."
    )
    assert paragraphs[3].startswith("This workspace has no backups configured")


def test_a_messenger_whose_own_bare_create_refuses_the_title_hands_the_create_to_the_programs_bare_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A released messenger behind an unreachable chat app runs ``mngr create`` under the title it was given, which
    no mngr name can hold; the program's own bare create names the agent so mngr takes it."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="Falling back to `mngr create`: could not connect to the chat app\n"
                "AgentName must be alphanumeric (with dashes and underscores allowed in the middle): "
                "'Update to v0.9.0'\n",
            )
        ],
    )

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert (result.outcome, result.chat_agent_name) == (UpdateArmOutcome.CREATED, "Update-to-v0-9-0")
    creates = [call for call in staged.recorded_calls() if call[:1] == ["create"]]
    assert len(creates) == 1
    assert creates[0][1] == "Update-to-v0-9-0"
    assert "display_name=Update to v0.9.0" in creates[0]


def test_a_chat_app_and_a_bare_create_that_both_fail_read_as_the_chat_app_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.script_tool(
        "system/scripts/message_chat.py",
        [ScriptedToolAnswer(exit_code=1, stderr="Falling back to `mngr create`: could not connect to the chat app\n")],
    )
    monkeypatch.setenv("STUB_EXIT_CREATE", "1")

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert (result.outcome, result.detail) == (UpdateArmOutcome.CHAT_APP_UNREACHABLE, "mngr create exited 1")
    assert staged.read_state("last-dispatch.json") is None


def test_a_taken_chat_name_gets_the_next_free_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Earlier runs' chats stay as history, so a second update to the same release needs its own name."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.set_agents([{"name": "Update-to-v090", "id": "agent-" + "b" * 32, "state": "WAITING"}])
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="The chat app did not create the chat: "
                "A chat named 'Update to v0.9.0 2' already exists; pick another name\n",
            ),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )

    _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert [_flag(create, "--name") for create in staged.chat_creates()] == [
        "Update to v0.9.0 2",
        "Update to v0.9.0 3",
    ]


def test_update_now_adopts_a_run_already_under_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {"chat_agent_name": "update-to-minds-v0-8-7", "started_at": _NOW - 600})
    staged.set_agents([{"name": "update-to-minds-v0-8-7", "id": "agent-" + "d" * 32, "state": "RUNNING"}])

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.ALREADY_RUNNING
    assert (result.chat_agent_name, result.chat_id) == ("update-to-minds-v0-8-7", "agent-" + "d" * 32)
    assert result.run_started_at == datetime.fromtimestamp(_NOW - 600, tz=timezone.utc)
    assert staged.chat_creates() == []


def test_update_now_adopts_a_chat_dispatched_moments_ago_that_has_not_recorded_its_run_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its lead takes the lease and records its start only minutes after its chat is made."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    chat_id = "agent-" + "e" * 32
    staged.write_state(
        "run.json", {"chat_agent_name": "update-to-minds-v0-8-7", "started_at": _NOW - 86400, "verdict": "UPDATED"}
    )
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": "update-to-minds-v0-9-0",
            "chat_id": chat_id,
            "started_by": "scheduled",
            "dispatched_at": _NOW - 120,
        },
    )
    staged.set_agents([{"name": "update-to-minds-v0-9-0", "id": chat_id, "state": "RUNNING"}])

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is UpdateArmOutcome.ALREADY_RUNNING
    assert (result.chat_agent_name, result.chat_id) == ("update-to-minds-v0-9-0", chat_id)
    # The dispatch's time, not the earlier run's start the record still holds.
    assert result.run_started_at == datetime.fromtimestamp(_NOW - 120, tz=timezone.utc)
    assert staged.chat_creates() == []


_DISPATCHED_CHAT = "update-to-minds-v0-9-0"
_DISPATCHED_CHAT_ID = "agent-" + "e" * 32


@pytest.mark.parametrize(
    ("dispatched_chat_id", "listed", "outcome"),
    [
        # Deleted: the create gave it an id, so it was there, and it is no longer listed by name or id.
        (_DISPATCHED_CHAT_ID, [], UpdateArmOutcome.CREATED),
        # Still being created: the chat app answered before it had an id, and it is not listed until it lands.
        ("", [], UpdateArmOutcome.ALREADY_RUNNING),
        # Renamed: listed under its new name, by the id the create gave it.
        (
            _DISPATCHED_CHAT_ID,
            [{"name": "my-update", "id": _DISPATCHED_CHAT_ID, "state": "RUNNING"}],
            UpdateArmOutcome.ALREADY_RUNNING,
        ),
    ],
)
def test_update_now_counts_a_chat_dispatched_moments_ago_only_while_it_can_still_be_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dispatched_chat_id: str,
    listed: list[dict[str, str]],
    outcome: UpdateArmOutcome,
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state(
        "run.json", {"chat_agent_name": "update-to-minds-v0-8-7", "started_at": _NOW - 86400, "verdict": "UPDATED"}
    )
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _DISPATCHED_CHAT,
            "chat_id": dispatched_chat_id,
            "started_by": "immediate",
            "dispatched_at": _NOW - 120,
        },
    )
    staged.set_agents(listed)

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is outcome
    assert len(staged.chat_creates()) == (1 if outcome is UpdateArmOutcome.CREATED else 0)


@pytest.mark.parametrize(
    ("lead_state", "has_cron", "outcome"),
    [
        # The watchdog brings a stopped lead back, so its run is still under way.
        ("STOPPED", True, UpdateArmOutcome.ALREADY_RUNNING),
        ("DONE", True, UpdateArmOutcome.ALREADY_RUNNING),
        # No cron, no watchdog: nothing will bring it back, so it does not hold a new run back.
        ("STOPPED", False, UpdateArmOutcome.CREATED),
        # Its chat was deleted: there is nothing to bring back.
        (None, True, UpdateArmOutcome.CREATED),
    ],
)
@pytest.mark.witnesses("workspace-updates.stopped-agent-revived", partial="update now's answer only")
@pytest.mark.witnesses("workspace-updates.gone-agent-closed-out", partial="a fresh start only")
def test_update_now_counts_a_stopped_lead_as_under_way_only_where_the_watchdog_revives_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lead_state: str | None,
    has_cron: bool,
    outcome: UpdateArmOutcome,
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=has_cron)
    staged.write_state("run.json", {"chat_agent_name": _DISPATCHED_CHAT, "started_at": _NOW - 600})
    staged.set_agents(
        [{"name": _DISPATCHED_CHAT, "id": _DISPATCHED_CHAT_ID, "state": lead_state}] if lead_state is not None else []
    )

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is outcome
    if outcome is UpdateArmOutcome.ALREADY_RUNNING:
        assert result.chat_id == _DISPATCHED_CHAT_ID


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        (
            "The chat app did not create the chat: Connect an AI provider to start chatting.\n",
            UpdateArmOutcome.NEEDS_SIGN_IN,
        ),
        ("The chat app did not create the chat: mngr create exited 1\n", UpdateArmOutcome.CREATE_FAILED),
        # A refusal can run long; the detail keeps its tail.
        ("The chat app did not create the chat: " + "a log line\n" * 200, UpdateArmOutcome.CREATE_FAILED),
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

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    result = parse_update_command_result(answer)
    assert result.outcome is outcome
    assert result.detail == stderr.strip()[-1000:]
    assert staged.read_state("last-dispatch.json") is None


def test_a_workspace_with_no_messenger_gets_a_bare_create_carrying_the_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_message_chat=False)

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is UpdateArmOutcome.CREATED
    creates = [call for call in staged.recorded_calls() if call[:1] == ["create"]]
    assert len(creates) == 1
    assert creates[0][1] == "Update-to-v0-9-0"
    assert "display_name=Update to v0.9.0" in creates[0]
    assert "auto_open=true" in creates[0]


def test_update_now_asks_an_older_templates_account_resolver_once_and_creates_on_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=False, has_message_chat=False)
    scripts_dir = staged.root / "system" / "scripts"
    (scripts_dir / "require_create_account.py").unlink()
    (scripts_dir / "default_account_args.py").write_text("")
    staged.script_program_on_path("uv", [ScriptedToolAnswer(stdout="--env\nCLAUDE_CONFIG_DIR=/home/user/acc/a1\n")])

    answer = _run_mode(staged, "now", build_now_arguments(_SETTINGS, ""), capsys)

    assert parse_update_command_result(answer).outcome is UpdateArmOutcome.CREATED
    assert staged.tool_calls("uv") == [["run", "python", "system/scripts/default_account_args.py", "claude"]]
    creates = [call for call in staged.recorded_calls() if call[:1] == ["create"]]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--env") + 1] == "CLAUDE_CONFIG_DIR=/home/user/acc/a1"


@pytest.mark.witnesses("workspace-updates.schedule-follows-the-app")
def test_reading_an_armed_schedule_refreshes_the_apps_fields_and_keeps_the_dispatchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "minds-v0.9.1", is_auto_armed=False), capsys)
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    staged.write_state(
        "schedule.json",
        {**schedule, "marked_chat": "update-to-minds-v0-9-1", "marked_at": _NOW, "last_skip_reason": "LEASE_HELD"},
    )
    staged.write_state("run.json", {"chat_agent_name": "update-to-minds-v0-9-1", "started_at": _NOW})
    moved = UpdateWorkspaceSettings(
        ceiling_ref="minds-v0.10.0",
        window=UtcUpdateWindow(start_minute_utc=21 * 60 + 30, length_minutes=4 * 60),
        is_backup_configured=False,
    )

    answer = _run_mode(staged, "read", build_read_arguments(moved, is_sign_in_checked=False), capsys)

    facts = parse_update_workspace_facts(answer)
    assert facts.schedule is not None
    assert (
        facts.schedule.ceiling_ref,
        facts.schedule.window_start_minute_utc,
        facts.schedule.window_length_minutes,
    ) == ("minds-v0.10.0", 21 * 60 + 30, 4 * 60)
    assert facts.schedule.override_ref == "minds-v0.9.1"
    assert facts.schedule.marked_chat_name == "update-to-minds-v0-9-1"
    assert facts.schedule.last_skip_reason == "LEASE_HELD"
    assert facts.run_status is not None and facts.run_status.chat_agent_name == "update-to-minds-v0-9-1"


@pytest.mark.parametrize(("override_ref", "target_ref"), (("", "minds-v0.9.0"), ("minds-v0.8.7", "minds-v0.8.7")))
def test_reading_after_a_dispatch_names_the_release_the_run_aimed_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override_ref: str,
    target_ref: str,
) -> None:
    """Auto-update holds back the release the last dispatch aimed at, so the app must read it from the record."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    _run_mode(staged, "now", build_now_arguments(_SETTINGS, override_ref), capsys)

    answer = _run_mode(staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    dispatch = parse_update_workspace_facts(answer).last_dispatch
    assert dispatch is not None
    assert dispatch.target_ref == target_ref


@pytest.mark.parametrize(
    ("dispatched_chat", "listed_chats", "is_services_agent_listed", "run_chat_id", "is_listing_expected"),
    (
        # A run started by hand in a chat: the listing names its chat.
        ("Update-to-v090", ("update-self-1330f8",), True, "agent-" + "c" * 32, True),
        # Its chat since deleted.
        ("Update-to-v090", (), True, "", True),
        # A listing that read the wrong host dir says nothing about the chat.
        ("Update-to-v090", (), False, None, True),
        # The last dispatch names the run's chat already, so nothing is listed.
        ("update-self-1330f8", ("update-self-1330f8",), True, None, False),
    ),
)
def test_reading_names_the_chat_of_a_run_the_last_dispatch_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dispatched_chat: str,
    listed_chats: tuple[str, ...],
    is_services_agent_listed: bool,
    run_chat_id: str | None,
    is_listing_expected: bool,
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {"chat_agent_name": "update-self-1330f8", "started_at": 1.0, "verdict": "STUCK"})
    staged.write_state("last-dispatch.json", {"schema": 1, "chat_agent_name": dispatched_chat, "chat_id": "agent-x"})
    staged.set_agents(
        [{"name": name, "id": "agent-" + "c" * 32, "state": "STOPPED"} for name in listed_chats],
        is_services_agent_listed=is_services_agent_listed,
    )

    answer = _run_mode(staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    assert parse_update_workspace_facts(answer).run_chat_id == run_chat_id
    is_listed = any(call[:1] == ["list"] for call in staged.recorded_calls())
    assert is_listed is is_listing_expected


def test_reading_a_workspace_with_nothing_armed_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)

    answer = _run_mode(staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    facts = parse_update_workspace_facts(answer)
    assert (facts.schedule, facts.run_status, facts.last_dispatch) == (None, None, None)
    assert not (staged.root / "data" / ".state" / "update-apply").exists()


@pytest.mark.parametrize("is_signed_in", [True, False])
def test_a_read_asked_about_the_account_says_whether_one_is_signed_in_with_nothing_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], is_signed_in: bool
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=is_signed_in)

    asked = _run_mode(staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=True), capsys)
    unasked = _run_mode(staged, "read", build_read_arguments(_SETTINGS, is_sign_in_checked=False), capsys)

    assert parse_update_workspace_facts(asked).is_signed_in is is_signed_in
    assert parse_update_workspace_facts(unasked).is_signed_in is None


def test_cancelling_deletes_the_schedule_and_says_whether_one_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=False), capsys)

    first = _run_mode(staged, "cancel", [], capsys)
    second = _run_mode(staged, "cancel", [], capsys)

    assert (first["is_cancelled"], second["is_cancelled"]) == (True, False)
    assert staged.read_state("schedule.json") is None


def test_the_shipped_command_runs_the_program_from_a_scratch_file_through_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What ``mngr exec`` runs: decode, run in the workspace root, remove the scratch file, keep the exit status."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", {"window_start_minute_utc": 9 * 60, "window_length_minutes": 3 * 60})
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


def test_the_dispatcher_an_arm_installs_runs_its_tick_on_its_own_from_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cron runs the installed archive with nothing beside it, as its line says: ``python3 <archive> tick``."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    _run_mode(staged, "arm", build_arm_arguments(_SETTINGS, "", is_auto_armed=False), capsys)
    lead_id = "agent-" + "f" * 32
    staged.write_state("run.json", {"chat_agent_name": "Update-to-v090", "started_at": _NOW})
    staged.set_agents([{"name": "Update-to-v090", "id": lead_id, "state": "STOPPED"}])

    completed = subprocess.run(
        ["python3", DISPATCHER_ARCHIVE_PATH, "tick"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=staged.root,
    )

    assert completed.returncode == 0, completed.stderr
    # The watchdog restarted the stopped lead; whether the tick also dispatched depends on the clock.
    messages = [call for call in staged.tool_calls("message_chat.py") if call[:1] != ["--create"]]
    assert [message[:2] for message in messages] == [[lead_id, "-m"]]


def test_the_programs_outcome_skip_and_started_by_strings_are_the_apps_enum_values() -> None:
    """They cross the process boundary as plain strings, so each must parse as the app's enum reads it."""
    outcomes = (
        update_protocol.OUTCOME_ARMED,
        update_protocol.OUTCOME_CREATED,
        update_protocol.OUTCOME_ALREADY_RUNNING,
        update_protocol.OUTCOME_NEEDS_SIGN_IN,
        update_protocol.OUTCOME_CHAT_APP_UNREACHABLE,
        update_protocol.OUTCOME_CREATE_FAILED,
        update_protocol.OUTCOME_NO_CRON,
        update_protocol.OUTCOME_TOO_OLD_TO_UPDATE,
    )
    skip_reasons = (
        update_protocol.SKIP_UPDATE_IN_FLIGHT,
        update_protocol.SKIP_LEASE_HELD,
        update_protocol.SKIP_NEEDS_SIGN_IN,
        update_protocol.SKIP_DISPATCH_FAILED,
        update_protocol.SKIP_WINDOW_CLOSED,
    )
    started_by = (update_protocol.STARTED_BY_SCHEDULED, update_protocol.STARTED_BY_IMMEDIATE)

    assert sorted(outcomes) == sorted(outcome.value for outcome in UpdateArmOutcome)
    assert sorted(skip_reasons) == sorted(reason.value for reason in UpdateSkipReason)
    # The app upper-cases the stamp it reads from ``last-dispatch.json``.
    assert sorted(value.upper() for value in started_by) == sorted(value.value for value in UpdateStartedBy)
