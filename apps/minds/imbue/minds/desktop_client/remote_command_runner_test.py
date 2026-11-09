import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.test_utils import is_process_with_marker_running
from imbue.concurrency_group.test_utils import poll_until
from imbue.minds.desktop_client.api_models import RemoteExecutionRequest
from imbue.minds.desktop_client.api_transport import ApiTransport
from imbue.minds.desktop_client.remote_command_runner import DEFAULT_TIMEOUT_SECONDS
from imbue.minds.desktop_client.remote_command_runner import EXIT_CODE_COMMAND_NOT_EXECUTABLE
from imbue.minds.desktop_client.remote_command_runner import EXIT_CODE_COMMAND_NOT_FOUND
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandAttribution
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandExecution
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandFinishedEvent
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandLog
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandRequestCode
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandRun
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandStartedEvent
from imbue.minds.desktop_client.remote_command_runner import describe_start_failure
from imbue.minds.desktop_client.remote_command_runner import resolve_remote_command
from imbue.minds.errors import RemoteCommandRequestError


def _execution(
    concurrency_group: ConcurrencyGroup,
    tmp_path: Path,
    command: tuple[str, ...],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    keepalive_interval_seconds: float = 30.0,
) -> RemoteCommandExecution:
    return RemoteCommandExecution(
        run=RemoteCommandRun(command=command, cwd=tmp_path, timeout_seconds=timeout_seconds),
        attribution=RemoteCommandAttribution(
            claimed_agent_id="agent-" + "a" * 32, workspace_id=None, transport=ApiTransport.GATEWAY
        ),
        log=RemoteCommandLog(events_path=tmp_path / "events" / "remote_execution" / "events.jsonl"),
        concurrency_group=concurrency_group,
        environment=dict(os.environ),
        keepalive_interval_seconds=keepalive_interval_seconds,
    )


def _events(lines: Iterator[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in lines]


def _text(events: list[dict[str, object]], stream: str) -> str:
    return "".join(str(event["data"]) for event in events if event["type"] == stream)


def _logged(tmp_path: Path) -> tuple[RemoteCommandStartedEvent, RemoteCommandFinishedEvent]:
    lines = (tmp_path / "events" / "remote_execution" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    return (
        RemoteCommandStartedEvent.model_validate_json(lines[0]),
        RemoteCommandFinishedEvent.model_validate_json(lines[1]),
    )


@pytest.mark.witnesses(
    "remote-execution.every-command-recorded", partial="the executor's record alone, with no caller to attribute it to"
)
def test_a_command_streams_both_pipes_and_its_exit_code(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("sh", "-c", "echo out; echo err >&2; exit 3"))

    events = _events(execution.stream_events())

    assert _text(events, "stdout") == "out\n"
    assert _text(events, "stderr") == "err\n"
    assert events[-1] == {"type": "exit", "code": 3, "timed_out": False}
    started, finished = _logged(tmp_path)
    assert started.execution_id == finished.execution_id
    assert started.command == ("sh", "-c", "echo out; echo err >&2; exit 3")
    assert started.claimed_agent_id == "agent-" + "a" * 32
    assert (finished.exit_code, finished.timed_out, finished.cancelled) == (3, False, False)
    assert (finished.stdout_bytes, finished.stderr_bytes) == (4, 4)


def test_a_command_past_its_timeout_is_killed_with_what_it_forked_and_reported_as_timed_out(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    marker = "sleep 36295"
    execution = _execution(
        root_concurrency_group, tmp_path, ("sh", "-c", f"echo begun; {marker} & wait"), timeout_seconds=0.5
    )

    events = _events(execution.stream_events())

    assert _text(events, "stdout") == "begun\n"
    assert events[-1] == {"type": "exit", "code": None, "timed_out": True}
    assert not is_process_with_marker_running(marker)
    _started, finished = _logged(tmp_path)
    assert (finished.exit_code, finished.timed_out, finished.cancelled) == (None, True, False)


def test_a_consumer_that_stops_reading_kills_the_command(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    marker = "sleep 36296"
    execution = _execution(root_concurrency_group, tmp_path, ("sh", "-c", f"echo begun; {marker}"))
    stream = execution.stream_events()
    assert json.loads(next(stream)) == {"type": "stdout", "data": "begun\n"}

    stream.close()

    assert not is_process_with_marker_running(marker)
    _started, finished = _logged(tmp_path)
    assert (finished.exit_code, finished.timed_out, finished.cancelled) == (None, False, True)


def test_a_consumer_that_leaves_after_the_command_ended_gets_its_real_exit_recorded(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The exit line is built after the last output line; a consumer gone between the two killed nothing."""
    execution = _execution(root_concurrency_group, tmp_path, ("sh", "-c", "echo begun; exit 5"))
    stream = execution.stream_events()
    assert json.loads(next(stream)) == {"type": "stdout", "data": "begun\n"}
    assert poll_until(lambda: root_concurrency_group.unfinished_processes == ())

    stream.close()

    _started, finished = _logged(tmp_path)
    assert (finished.exit_code, finished.timed_out, finished.cancelled) == (5, False, False)


def test_the_concurrency_group_shutting_down_kills_the_command(tmp_path: Path) -> None:
    marker = "sleep 36297"
    with ConcurrencyGroup(name="remote-command-shutdown-36297") as group:
        execution = _execution(group, tmp_path, ("sh", "-c", f"echo begun; {marker}"))
        stream = execution.stream_events()
        assert json.loads(next(stream))["data"] == "begun\n"
        group.shutdown()
        events = _events(stream)

    assert events[-1] == {"type": "exit", "code": None, "timed_out": False}
    assert not is_process_with_marker_running(marker)
    _started, finished = _logged(tmp_path)
    assert finished.cancelled is True


def test_a_missing_executable_is_reported_on_the_stream_not_as_an_error(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("definitely-not-a-command-36287",))

    events = _events(execution.stream_events())

    assert events == [
        {"type": "stderr", "data": "command not found: definitely-not-a-command-36287\n"},
        {"type": "exit", "code": EXIT_CODE_COMMAND_NOT_FOUND, "timed_out": False},
    ]
    _started, finished = _logged(tmp_path)
    assert finished.exit_code == EXIT_CODE_COMMAND_NOT_FOUND


def test_a_file_that_cannot_run_is_reported_with_the_shells_code(tmp_path: Path) -> None:
    script = tmp_path / "not-executable-36288.sh"
    script.write_text("#!/bin/sh\necho never\n")

    failure = describe_start_failure(str(script), {}, tmp_path)

    assert failure is not None
    assert failure.exit_code == EXIT_CODE_COMMAND_NOT_EXECUTABLE
    assert describe_start_failure("sh", dict(os.environ), tmp_path) is None


def test_a_script_whose_interpreter_is_missing_is_reported_on_the_stream_not_raised(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The pre-check passes (the file exists and is executable), so the failure comes from the spawn itself."""
    script = tmp_path / "orphaned-36298.sh"
    script.write_text("#!/definitely/missing/interpreter-36298\necho never\n")
    script.chmod(0o700)
    execution = _execution(root_concurrency_group, tmp_path, (str(script),))

    events = _events(execution.stream_events())

    assert [event["type"] for event in events] == ["stderr", "exit"]
    assert "No such file or directory" in str(events[0]["data"])
    assert events[-1] == {"type": "exit", "code": EXIT_CODE_COMMAND_NOT_FOUND, "timed_out": False}
    _started, finished = _logged(tmp_path)
    assert finished.exit_code == EXIT_CODE_COMMAND_NOT_FOUND
    assert finished.stderr_bytes == len(str(events[0]["data"]).encode())


def test_a_relative_executable_is_resolved_against_the_commands_directory(tmp_path: Path) -> None:
    """A spawn looks for ``./x`` in the command's cwd, so the pre-check must look there too."""
    script = tmp_path / "runnable-36294.sh"
    script.write_text("#!/bin/sh\necho ran\n")
    script.chmod(0o700)

    assert describe_start_failure("./runnable-36294.sh", {}, tmp_path) is None
    missing = describe_start_failure("./runnable-36294.sh", {}, tmp_path / "elsewhere")
    assert missing is not None
    assert missing.exit_code == EXIT_CODE_COMMAND_NOT_FOUND


def test_a_silent_command_keeps_the_stream_alive(root_concurrency_group: ConcurrencyGroup, tmp_path: Path) -> None:
    execution = _execution(
        root_concurrency_group, tmp_path, ("sh", "-c", "sleep 0.6; echo done"), keepalive_interval_seconds=0.05
    )

    events = _events(execution.stream_events())

    assert any(event["type"] == "keepalive" for event in events)
    assert [event for event in events if event["type"] == "stdout"] == [{"type": "stdout", "data": "done\n"}]
    assert events[-1]["type"] == "exit"


def test_binary_output_is_decoded_with_replacement(root_concurrency_group: ConcurrencyGroup, tmp_path: Path) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("sh", "-c", r"printf '\377\376ok\n'"))

    events = _events(execution.stream_events())

    stdout = _text(events, "stdout")
    assert stdout.endswith("ok\n")
    assert "�" in stdout


def test_an_unterminated_last_line_still_arrives(root_concurrency_group: ConcurrencyGroup, tmp_path: Path) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("sh", "-c", "printf 'no newline'"))

    events = _events(execution.stream_events())

    assert _text(events, "stdout") == "no newline"
    assert events[-1] == {"type": "exit", "code": 0, "timed_out": False}


def test_the_command_runs_in_the_resolved_directory(root_concurrency_group: ConcurrencyGroup, tmp_path: Path) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("pwd",))

    events = _events(execution.stream_events())

    assert _text(events, "stdout").strip() == str(tmp_path.resolve())


def test_the_log_file_is_created_readable_by_the_user_alone(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    execution = _execution(root_concurrency_group, tmp_path, ("true",))

    list(execution.stream_events())

    mode = stat.S_IMODE((tmp_path / "events" / "remote_execution" / "events.jsonl").stat().st_mode)
    assert mode == 0o600


def test_resolve_fills_the_defaults_and_expands_the_home_directory(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()

    run = resolve_remote_command("ls", RemoteExecutionRequest(cwd="~/notes"), home_dir=tmp_path)
    bare_home = resolve_remote_command("ls", RemoteExecutionRequest(cwd="~"), home_dir=tmp_path)
    unset = resolve_remote_command("ls", RemoteExecutionRequest(), home_dir=tmp_path)

    assert run == RemoteCommandRun(command=("ls",), cwd=tmp_path / "notes", timeout_seconds=DEFAULT_TIMEOUT_SECONDS)
    assert bare_home.cwd == tmp_path
    assert unset.cwd == tmp_path
    assert resolve_remote_command("ls", RemoteExecutionRequest(timeout_seconds=7), tmp_path).timeout_seconds == 7


@pytest.mark.parametrize("program", ["", "bin/tool", "./tool", "../tool", "~tool", "-rf", " ls", "a\tb"])
def test_resolve_refuses_a_program_outside_the_grant_grammar(tmp_path: Path, program: str) -> None:
    """The desktop holds the program to the grammar grants are minted under, whatever the URL carried."""
    with pytest.raises(RemoteCommandRequestError) as caught:
        resolve_remote_command(program, RemoteExecutionRequest(), home_dir=tmp_path)

    assert caught.value.field == "program"
    assert caught.value.code == RemoteCommandRequestCode.INVALID_PROGRAM


@pytest.mark.parametrize("cwd", ["/definitely/missing/36288", "relative/path", "~/missing-36289"])
def test_resolve_refuses_a_working_directory_that_is_not_one(tmp_path: Path, cwd: str) -> None:
    with pytest.raises(RemoteCommandRequestError) as caught:
        resolve_remote_command("ls", RemoteExecutionRequest(cwd=cwd), home_dir=tmp_path)

    assert caught.value.field == "cwd"


def test_a_home_rooted_program_is_expanded_and_the_record_says_where_it_resolved(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """An agent names a script it wrote as ``~/...``; the desktop runs the file and records which one."""
    scripts = tmp_path / ".studio" / "custom-scripts"
    scripts.mkdir(parents=True)
    script = scripts / "say-36301.sh"
    script.write_text("#!/bin/sh\necho ran from script\n")
    script.chmod(0o700)
    run = resolve_remote_command(
        "~/.studio/custom-scripts/say-36301.sh", RemoteExecutionRequest(args=("ignored",)), home_dir=tmp_path
    )
    execution = _execution(root_concurrency_group, tmp_path, run.command)

    events = _events(execution.stream_events())

    assert run.command == (str(script), "ignored")
    assert _text(events, "stdout") == "ran from script\n"
    started, _finished = _logged(tmp_path)
    assert started.command == (str(script), "ignored")
    assert started.resolved_program == str(script)


def test_a_bare_name_resolves_on_the_environments_path_and_the_record_says_where(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "tool-36302"
    tool.write_text("#!/bin/sh\necho tool ran\n")
    tool.chmod(0o700)
    execution = RemoteCommandExecution(
        run=RemoteCommandRun(command=("tool-36302",), cwd=tmp_path, timeout_seconds=DEFAULT_TIMEOUT_SECONDS),
        attribution=RemoteCommandAttribution(claimed_agent_id=None, workspace_id=None, transport=ApiTransport.GATEWAY),
        log=RemoteCommandLog(events_path=tmp_path / "events" / "remote_execution" / "events.jsonl"),
        concurrency_group=root_concurrency_group,
        environment={**os.environ, "PATH": str(bin_dir)},
    )

    events = _events(execution.stream_events())

    assert _text(events, "stdout") == "tool ran\n"
    started, _finished = _logged(tmp_path)
    assert started.resolved_program == str(tool)
