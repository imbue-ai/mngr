import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_NATIVE_TYPE_MAX_LENGTH
from imbue.mngr.hosts.background_tasks import build_lifecycle_probe_read_command
from imbue.mngr.hosts.background_tasks import determine_agent_probe_result
from imbue.mngr.hosts.background_tasks import parse_background_tasks
from imbue.mngr.hosts.background_tasks import parse_lifecycle_probe_reading
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import BackgroundTask
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.primitives import LifecycleProbeReading
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.utils.testing import RAW_LINE_SEPARATOR_BY_NAME

# More empty records than the scan is given file descriptors.
_EMPTY_RECORD_COUNT: Final[int] = 60
_SCAN_FILE_DESCRIPTOR_LIMIT: Final[int] = 40


def test_parse_lifecycle_probe_reading_splits_the_turn_from_the_records() -> None:
    reading = parse_lifecycle_probe_reading(
        "ACTIVE=true\nBACKGROUND_TASK=bg-1\tseen:1767355200\nBACKGROUND_TASK=wake-2\tseen:1767355260\n"
    )

    assert reading == LifecycleProbeReading(
        is_active=True,
        raw_background_tasks="bg-1\tseen:1767355200\nwake-2\tseen:1767355260",
    )


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param("ACTIVE=\n", id="marker_absent"),
        pytest.param("ACTIVE=false\n", id="marker_absent_as_the_listing_spells_it"),
        pytest.param("", id="nothing_read"),
        pytest.param("awk: cannot open /agents/a/background_tasks/*\n", id="only_noise"),
    ],
)
def test_parse_lifecycle_probe_reading_reads_no_turn_and_no_record(stdout: str) -> None:
    """Anything the read did not label is not a marker and not a record."""
    assert parse_lifecycle_probe_reading(stdout) == LifecycleProbeReading()


def test_build_probe_read_command_reads_the_marker_and_the_records_of_one_agent() -> None:
    """One call collects both halves, and survives a record read that exits non-zero."""
    command = build_lifecycle_probe_read_command("/state/agents/a")

    assert "/state/agents/a/active" in command
    assert "/state/agents/a/background_tasks/*" in command
    assert command.endswith("; :")


def test_build_probe_read_command_quotes_a_directory_that_needs_it() -> None:
    command = build_lifecycle_probe_read_command("/state/with a space")

    assert "'/state/with a space'/active" in command


def test_build_probe_read_command_reads_a_record_that_sorts_after_many_empty_ones(tmp_path: Path) -> None:
    """The scan closes every file it opens, so empty records do not use up its descriptors."""
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir()
    for index in range(_EMPTY_RECORD_COUNT):
        (record_dir / f"empty-{index}").touch()
    (record_dir / "zz-task").write_text("seen:1767358800\n")

    command = f"ulimit -n {_SCAN_FILE_DESCRIPTOR_LIMIT}; {build_lifecycle_probe_read_command(str(tmp_path))}"
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30)

    background_tasks = parse_background_tasks(parse_lifecycle_probe_reading(result.stdout).raw_background_tasks)
    assert len(background_tasks) == _EMPTY_RECORD_COUNT + 1, result.stderr
    assert (background_tasks[-1].name, background_tasks[-1].last_seen_at) == (
        "zz-task",
        datetime(2026, 1, 2, 13, 0, 0, tzinfo=timezone.utc),
    )


def _scan_records_in(tmp_path: Path, content_by_name: dict[str, bytes]) -> tuple[BackgroundTask, ...]:
    """Write each record file, then read the directory back through the production scan."""
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir(exist_ok=True)
    for name, content in content_by_name.items():
        (record_dir / name).write_bytes(content)
    result = subprocess.run(
        ["sh", "-c", build_lifecycle_probe_read_command(str(tmp_path))], capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return parse_background_tasks(parse_lifecycle_probe_reading(result.stdout.decode()).raw_background_tasks)


def test_a_scanned_record_counts_whatever_its_file_holds(tmp_path: Path) -> None:
    records = _scan_records_in(
        tmp_path,
        {
            "task-a-empty": b"",
            "task-b-blank-line": b"\n",
            "task-c-no-token": b"forever\n",
            "task-d-unknown-fields": b"pid:4821 reason:compaction\n",
            "task-e-no-newline": b"seen:1767355200 kind:SHELL",
            "task-f-current": b"seen:1767355200 kind:SUBAGENT native_type:cloud%20session\n",
        },
    )

    seen_at = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert records == (
        BackgroundTask(name="task-a-empty"),
        BackgroundTask(name="task-b-blank-line"),
        BackgroundTask(name="task-c-no-token"),
        BackgroundTask(name="task-d-unknown-fields"),
        BackgroundTask(name="task-e-no-newline", kind=BackgroundTaskKind.SHELL, last_seen_at=seen_at),
        BackgroundTask(
            name="task-f-current", kind=BackgroundTaskKind.SUBAGENT, native_type="cloud session", last_seen_at=seen_at
        ),
    )


@pytest.mark.parametrize("separator", RAW_LINE_SEPARATOR_BY_NAME.values(), ids=RAW_LINE_SEPARATOR_BY_NAME.keys())
def test_a_scanned_record_holding_a_raw_line_separator_is_one_record(separator: str, tmp_path: Path) -> None:
    line = f"seen:1767355200 kind:SHELL native_type:a{separator}BACKGROUND_TASK=task-forged\tseen:1 since:1767351600\n"

    records = _scan_records_in(tmp_path, {"task-real": line.encode()})

    assert [(record.name, record.started_at) for record in records] == [
        ("task-real", datetime(2026, 1, 2, 11, 0, 0, tzinfo=timezone.utc))
    ]


@pytest.mark.parametrize(
    ("directory_name", "expected_state", "expected_names"),
    [
        pytest.param("background_tasks", AgentLifecycleState.RUNNING_BACKGROUND, ["task-abc"], id="background_tasks"),
        pytest.param("leases", AgentLifecycleState.WAITING, [], id="leases"),
    ],
)
def test_the_probe_command_reads_the_records_under_background_tasks_and_no_other_directory(
    directory_name: str, expected_state: AgentLifecycleState, expected_names: list[str], tmp_path: Path
) -> None:
    (tmp_path / directory_name).mkdir()
    (tmp_path / directory_name / "task-abc").write_text("seen:1767355200 kind:SHELL native_type:shell\n")

    result = subprocess.run(
        ["sh", "-c", build_lifecycle_probe_read_command(str(tmp_path))], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr
    probe = _probe(parse_lifecycle_probe_reading(result.stdout))
    assert (probe.state, [background_task.name for background_task in probe.background_tasks]) == (
        expected_state,
        expected_names,
    )


def test_a_scan_counts_neither_a_dot_named_file_nor_a_directory(tmp_path: Path) -> None:
    (tmp_path / BACKGROUND_TASK_DIR_NAME / "task-directory").mkdir(parents=True)

    records = _scan_records_in(tmp_path, {".task-abc.4821.0.tmp": b"seen:1767355200\n", "task-abc": b""})

    assert records == (BackgroundTask(name="task-abc"),)


def test_parse_background_tasks_reads_every_field_a_record_states() -> None:
    background_tasks = parse_background_tasks(
        "task-1\tseen:1767355200 kind:SUBAGENT native_type:cloud%20session since:1767351600\n"
    )

    assert background_tasks == (
        BackgroundTask(
            name="task-1",
            kind=BackgroundTaskKind.SUBAGENT,
            native_type="cloud session",
            started_at=datetime(2026, 1, 2, 11, 0, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
        ),
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("task-1\t\n", id="no_fields"),
        pytest.param("task-1\n", id="no_fields_and_no_separator"),
        pytest.param("task-1\tpid:4821 reason:compaction\n", id="only_fields_this_reader_does_not_know"),
        pytest.param("task-1\tforever\n", id="no_token_at_all"),
    ],
)
def test_parse_background_tasks_counts_a_record_that_states_no_field_it_knows(raw: str) -> None:
    assert parse_background_tasks(raw) == (BackgroundTask(name="task-1"),)


def test_parse_background_tasks_keeps_the_first_value_given_for_a_key() -> None:
    background_tasks = parse_background_tasks("task-1\tseen:1767355200 kind:SHELL seen:1767358800 kind:MONITOR\n")

    assert [(background_task.kind, background_task.last_seen_at) for background_task in background_tasks] == [
        (BackgroundTaskKind.SHELL, datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc))
    ]


@pytest.mark.parametrize(
    "whitespace",
    [
        pytest.param("\t", id="tab"),
        pytest.param("\r", id="carriage_return"),
        pytest.param("\x0c", id="form_feed"),
        pytest.param("\u00a0", id="no_break_space"),
        pytest.param("\u2028", id="line_separator"),
    ],
)
def test_parse_background_tasks_ends_a_token_only_at_a_space(whitespace: str) -> None:
    background_tasks = parse_background_tasks(f"task-1\tnative_type:a{whitespace}seen:1767355200 since:1767351600\n")

    assert [(background_task.last_seen_at, background_task.started_at) for background_task in background_tasks] == [
        (None, datetime(2026, 1, 2, 11, 0, 0, tzinfo=timezone.utc))
    ]


@pytest.mark.parametrize(
    ("raw_native_type", "native_type"),
    [
        pytest.param("caf%C3%A9%20%E2%98%95", "caf\u00e9 \u2615", id="utf8_bytes"),
        pytest.param("100%25%20%2541", "100% %41", id="an_escaped_percent_is_decoded_once"),
        pytest.param("seen%3A9999999999", "seen:9999999999", id="a_colon"),
        pytest.param("50%zz%2", "50%zz%2", id="a_percent_that_starts_no_escape_stands_for_itself"),
        pytest.param("a%FFb", "a\ufffdb", id="invalid_utf8_is_replaced"),
        pytest.param("bad/type", "bad/type", id="a_byte_a_writer_should_have_escaped"),
        pytest.param(
            "a%0Ab%09c%0Dd%00e%1Bf%7Fg%C2%85h",
            "a\ufffdb\ufffdc\ufffdd\ufffde\ufffdf\ufffdg\ufffdh",
            id="control_characters_are_replaced",
        ),
        pytest.param("a%E2%80%A8b%E2%80%A9c", "a\ufffdb\ufffdc", id="line_separators_are_replaced"),
        pytest.param("x" * 2000, "x" * BACKGROUND_TASK_NATIVE_TYPE_MAX_LENGTH, id="a_long_native_type_is_cut"),
        pytest.param(
            "%C3%A9" * 2000, "\u00e9" * BACKGROUND_TASK_NATIVE_TYPE_MAX_LENGTH, id="the_cut_counts_characters"
        ),
    ],
)
def test_parse_background_tasks_publishes_a_native_type_decoded_and_safe_to_print(
    raw_native_type: str, native_type: str
) -> None:
    background_tasks = parse_background_tasks(f"task-1\tseen:1767355200 kind:OTHER native_type:{raw_native_type}\n")

    assert [background_task.native_type for background_task in background_tasks] == [native_type]


@pytest.mark.parametrize("key", ["seen", "since"])
@pytest.mark.parametrize(
    "raw_instant",
    [
        pytest.param("", id="empty"),
        pytest.param("earlier", id="not_a_number"),
        pytest.param("-1767355200", id="negative"),
        pytest.param("1767355200.5", id="fractional"),
        pytest.param("\u00b2", id="superscript_digit"),
        pytest.param("\u0661\u0662\u0663", id="digits_outside_ascii"),
        pytest.param("999999999999", id="past_the_last_year_a_datetime_holds"),
        pytest.param("99999999999999999999", id="past_the_end_of_time"),
        pytest.param("9" * 5000, id="longer_than_int_parses"),
    ],
)
def test_parse_background_tasks_reads_an_instant_that_is_not_a_valid_number_as_none(
    key: str, raw_instant: str
) -> None:
    background_tasks = parse_background_tasks(f"task-1\tkind:SHELL {key}:{raw_instant}\n")

    assert background_tasks == (BackgroundTask(name="task-1", kind=BackgroundTaskKind.SHELL),)


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("seen:1767355200", id="no_kind"),
        pytest.param("seen:1767355200 kind:", id="empty_kind"),
        pytest.param("seen:1767355200 kind:DAEMON", id="a_kind_this_reader_does_not_know"),
        pytest.param("seen:1767355200 kind:subagent", id="a_kind_name_in_another_case"),
    ],
)
def test_parse_background_tasks_reads_a_kind_it_cannot_place_as_other(first_line: str) -> None:
    background_tasks = parse_background_tasks(f"task-1\t{first_line}\n")

    assert [
        (background_task.name, background_task.kind, background_task.native_type)
        for background_task in background_tasks
    ] == [("task-1", BackgroundTaskKind.OTHER, "")]


@pytest.mark.parametrize("kind", list(BackgroundTaskKind))
def test_parse_background_tasks_reads_a_kind_by_its_exact_name(kind: BackgroundTaskKind) -> None:
    background_tasks = parse_background_tasks(f"task-1\tkind:{kind.value}\n")

    assert [background_task.kind for background_task in background_tasks] == [kind]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("\n", id="blank_line"),
        pytest.param("\tseen:1767355200\n", id="no_name"),
        pytest.param("bad name\tseen:1767355200\n", id="name_outside_the_grammar"),
        pytest.param(".task-1.4821.0.tmp\tseen:1767355200\n", id="dot_named_staging_file"),
    ],
)
def test_parse_background_tasks_drops_a_line_whose_name_is_not_a_record_name(raw: str) -> None:
    assert parse_background_tasks(raw) == ()


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("bg\t1", id="holds_a_tab"),
        pytest.param(".bg-1", id="starts_with_a_dot"),
    ],
)
def test_background_task_refuses_a_name_no_record_file_can_have(name: str) -> None:
    with pytest.raises(ValueError):
        BackgroundTask(name=name)


def _probe(
    reading: LifecycleProbeReading,
    is_blocked_on_dialog: bool = False,
    tmux_info: str | None = "0|claude|123",
    ps_output: str | None = "",
) -> LifecycleProbeResult:
    return determine_agent_probe_result(
        tmux_info=tmux_info,
        is_blocked_on_dialog=is_blocked_on_dialog,
        expected_process_name="claude",
        ps_output=ps_output,
        reading=reading,
    )


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("seen:1767358800 kind:SHELL native_type:shell", id="seen_recently"),
        pytest.param("seen:1 kind:SHELL native_type:shell", id="seen_decades_ago"),
        pytest.param("pid:4821", id="only_a_field_this_reader_does_not_know"),
        pytest.param("", id="no_fields"),
    ],
)
def test_a_turn_that_ended_with_a_record_is_running_background_whatever_the_record_says(first_line: str) -> None:
    probe = _probe(LifecycleProbeReading(is_active=False, raw_background_tasks=f"task-1\t{first_line}"))

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND
    assert [background_task.name for background_task in probe.background_tasks] == ["task-1"]


def test_a_background_working_probe_publishes_what_each_record_states() -> None:
    probe = _probe(
        LifecycleProbeReading(
            is_active=False,
            raw_background_tasks="task-1\tseen:1767358800 kind:MONITOR native_type:monitor\ntask-2\tpid:4821",
        )
    )

    assert probe.state is AgentLifecycleState.RUNNING_BACKGROUND
    assert probe.background_tasks == (
        BackgroundTask(
            name="task-1",
            kind=BackgroundTaskKind.MONITOR,
            native_type="monitor",
            last_seen_at=datetime(2026, 1, 2, 13, 0, 0, tzinfo=timezone.utc),
        ),
        BackgroundTask(name="task-2"),
    )


def test_a_background_working_probe_carries_the_pid_the_process_table_gave_it() -> None:
    probe = _probe(
        LifecycleProbeReading(is_active=False, raw_background_tasks="task-1\tseen:1767358800"),
        ps_output="123 1 claude\n",
    )

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND
    assert probe.pid == 123


def test_a_live_agent_whose_type_is_unknown_keeps_the_state_the_marker_gave_it() -> None:
    """Records are read only where the state is WAITING, so an agent of unknown type reports none."""
    probe = determine_agent_probe_result(
        tmux_info="0|python3|123",
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output="",
        reading=LifecycleProbeReading(is_active=True, raw_background_tasks="task-1\tseen:1767358800"),
        is_agent_type_known=False,
    )

    assert probe.state == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE
    assert probe.background_tasks == ()


def test_an_open_turn_is_running_whatever_it_left_outstanding() -> None:
    probe = _probe(LifecycleProbeReading(is_active=True, raw_background_tasks="task-1\tseen:1767358800"))

    assert probe.state == AgentLifecycleState.RUNNING


def test_a_dialog_outranks_work_the_agent_left_running() -> None:
    """An agent a dialog holds reads WAITING and reports no background tasks."""
    reading = LifecycleProbeReading(is_active=True, raw_background_tasks="task-1\tseen:1767358800")

    probe = _probe(reading, is_blocked_on_dialog=True)

    assert probe.state == AgentLifecycleState.WAITING
    assert probe.background_tasks == ()


def test_a_turn_that_ended_with_nothing_outstanding_is_waiting() -> None:
    probe = _probe(LifecycleProbeReading(is_active=False))

    assert probe.state == AgentLifecycleState.WAITING
    assert probe.background_tasks == ()


def test_a_record_holds_without_a_process_table() -> None:
    probe = _probe(
        LifecycleProbeReading(is_active=False, raw_background_tasks="task-1\tseen:1767358800"), ps_output=None
    )

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND


@pytest.mark.parametrize(
    ("tmux_info", "expected_state"),
    [
        pytest.param(None, AgentLifecycleState.STOPPED, id="no_session"),
        pytest.param("1|bash|4242", AgentLifecycleState.DONE, id="pane_process_exited"),
    ],
)
def test_a_record_cannot_revive_an_agent_that_is_not_live(
    tmux_info: str | None, expected_state: AgentLifecycleState
) -> None:
    """Whatever its directory still holds, an agent whose process is gone is carrying nothing."""
    probe = _probe(
        LifecycleProbeReading(is_active=False, raw_background_tasks="task-1\tseen:1767358800"),
        tmux_info=tmux_info,
    )

    assert probe.state == expected_state
    assert probe.background_tasks == ()
