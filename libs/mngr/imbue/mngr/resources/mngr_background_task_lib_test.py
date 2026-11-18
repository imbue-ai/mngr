"""Unit tests for mngr_background_task_lib.sh, which read each record it writes back through the Python parser."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final
from urllib.parse import quote

import pytest

from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_KEEPALIVE_ENV_VAR
from imbue.mngr.hosts.background_tasks import DEFAULT_BACKGROUND_TASK_KEEPALIVE_SECONDS
from imbue.mngr.primitives import BackgroundTask
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.utils.testing import HOSTILE_NATIVE_TYPES
from imbue.mngr.utils.testing import read_background_task_records_in

_LIB: Path = Path(__file__).parent / "mngr_background_task_lib.sh"
_BASH: Final[tuple[str, ...]] = ("bash",)
_POSIX_SHELLS: Final[tuple[tuple[str, ...], ...]] = (("sh",), ("dash",), ("busybox", "sh"))
_ALL_SHELLS: Final[tuple[tuple[str, ...], ...]] = (_BASH, *_POSIX_SHELLS)
# How far the test's clock and the library's may read apart.
_CLOCK_SLACK: Final[timedelta] = timedelta(seconds=30)
# A locale whose multibyte encoding is not UTF-8.
_SHIFT_JIS_LOCALE: Final[str] = "ja_JP.SJIS"
# The characters jq before 1.7 leaves unescaped in @uri, with their escapes.
_JQ_LEGACY_UNESCAPED: Final[dict[str, str]] = {"!": "%21", "*": "%2A", "'": "%27", "(": "%28", ")": "%29"}
# Maps each ASCII digit to the Arabic-Indic digit of the same value.
_ARABIC_INDIC_DIGITS: Final[dict[int, int]] = {ord("0") + digit: 0x0660 + digit for digit in range(10)}
# Every external program the library runs except od.
_PROGRAMS_OTHER_THAN_OD: Final[tuple[str, ...]] = ("awk", "date", "mkdir", "mv", "rm", "tr")


def _run_lib(
    state_dir: Path | None,
    body: str,
    shell: Sequence[str] = _BASH,
    arguments: Sequence[str] = (),
    heartbeat_window_override: str | None = None,
    locale: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the library and run ``body`` with ``arguments`` as its positional parameters."""
    # Unset because the subprocess would otherwise inherit them from an mngr agent running these tests.
    state_dir_setup = (
        f"export MNGR_AGENT_STATE_DIR={shlex.quote(str(state_dir))}\n"
        if state_dir is not None
        else "unset MNGR_AGENT_STATE_DIR\n"
    )
    window_setup = (
        f"export {BACKGROUND_TASK_KEEPALIVE_ENV_VAR}={shlex.quote(heartbeat_window_override)}\n"
        if heartbeat_window_override is not None
        else f"unset {BACKGROUND_TASK_KEEPALIVE_ENV_VAR}\n"
    )
    script = f"{state_dir_setup}{window_setup}. {shlex.quote(str(_LIB))}\n{body}"
    env = dict(os.environ) if locale is None else {**os.environ, "LC_ALL": locale}
    return subprocess.run(
        [*shell, "-c", script, "lib-test", *arguments], capture_output=True, text=True, timeout=30, env=env
    )


def _read_records(state_dir: Path) -> tuple[BackgroundTask, ...]:
    return read_background_task_records_in(state_dir / BACKGROUND_TASK_DIR_NAME)


def _skip_unless_installed(shell: Sequence[str]) -> None:
    if shutil.which(shell[0]) is None:
        pytest.skip(f"{shell[0]} is not installed")


def test_a_written_record_is_seen_now_and_states_nothing_else(tmp_path: Path) -> None:
    written_at = datetime.now(timezone.utc)

    result = _run_lib(tmp_path, "mngr_background_task_write task-abc")
    assert result.returncode == 0, result.stderr

    records = _read_records(tmp_path)

    assert [(record.name, record.kind, record.native_type, record.started_at) for record in records] == [
        ("task-abc", BackgroundTaskKind.OTHER, "", None)
    ]
    assert records[0].last_seen_at is not None
    assert abs(records[0].last_seen_at - written_at) <= _CLOCK_SLACK


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
def test_records_are_written_and_cleared_under_background_tasks(shell: Sequence[str], tmp_path: Path) -> None:
    _skip_unless_installed(shell)

    result = _run_lib(
        tmp_path,
        "mngr_background_task_dir\n"
        "mngr_background_task_write task-abc SHELL shell\n"
        "mngr_background_task_write watch-xyz\n"
        "mngr_background_task_clear watch-",
        shell,
    )

    record_dir = tmp_path / "background_tasks"
    assert (result.returncode, result.stdout, result.stderr) == (0, str(record_dir), "")
    assert [path.name for path in record_dir.iterdir()] == ["task-abc"]
    assert not (tmp_path / "leases").exists()


def test_a_written_record_carries_the_kind_and_native_type_it_was_given(tmp_path: Path) -> None:
    _run_lib(tmp_path, "mngr_background_task_write task-abc SUBAGENT 'cloud session'")

    records = _read_records(tmp_path)

    assert [(record.name, record.kind, record.native_type) for record in records] == [
        ("task-abc", BackgroundTaskKind.SUBAGENT, "cloud session")
    ]


def test_a_written_record_never_states_a_deadline(tmp_path: Path) -> None:
    _run_lib(tmp_path, "mngr_background_task_write task-abc SHELL shell")

    line = (tmp_path / BACKGROUND_TASK_DIR_NAME / "task-abc").read_text()

    assert [token.partition(":")[0] for token in line.split()] == ["seen", "kind", "native_type"]
    assert line.endswith("\n") and line.count("\n") == 1


def _write_hostile_native_types(state_dir: Path, shell: Sequence[str], locale: str | None = None) -> list[str]:
    """Write one record per hostile native type; return what follows each one's `seen` token, in order."""
    body = (
        "index=0\n"
        'for native_type in "$@"; do\n'
        '    mngr_background_task_write "task-$index" OTHER "$native_type"\n'
        "    index=$((index + 1))\n"
        "done"
    )
    result = _run_lib(state_dir, body, shell, arguments=HOSTILE_NATIVE_TYPES, locale=locale)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    record_dir = state_dir / BACKGROUND_TASK_DIR_NAME
    return [(record_dir / f"task-{index}").read_text().split(" ", 1)[1] for index in range(len(HOSTILE_NATIVE_TYPES))]


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
def test_a_native_type_is_written_as_python_would_quote_it(shell: Sequence[str], tmp_path: Path) -> None:
    _skip_unless_installed(shell)

    assert _write_hostile_native_types(tmp_path, shell) == [
        f"kind:OTHER native_type:{quote(native_type, safe='')}\n" for native_type in HOSTILE_NATIVE_TYPES
    ]


def _is_locale_installed(locale: str) -> bool:
    result = subprocess.run(["locale", "-a"], capture_output=True, text=True, errors="replace", timeout=30)
    return locale.lower() in result.stdout.lower().split()


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
def test_a_native_type_is_written_the_same_in_a_locale_that_is_not_utf8(shell: Sequence[str], tmp_path: Path) -> None:
    _skip_unless_installed(shell)
    if not _is_locale_installed(_SHIFT_JIS_LOCALE):
        pytest.skip(f"the {_SHIFT_JIS_LOCALE} locale is not installed")

    assert _write_hostile_native_types(tmp_path, shell, locale=_SHIFT_JIS_LOCALE) == [
        f"kind:OTHER native_type:{quote(native_type, safe='')}\n" for native_type in HOSTILE_NATIVE_TYPES
    ]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_a_native_type_is_written_as_jq_would_uri_encode_it(tmp_path: Path) -> None:
    jq = subprocess.run(
        ["jq", "-r", ".[] | @uri"],
        input=json.dumps(list(HOSTILE_NATIVE_TYPES)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert jq.returncode == 0, jq.stderr
    encoded_by_jq = [
        "".join(_JQ_LEGACY_UNESCAPED.get(character, character) for character in line)
        for line in jq.stdout.splitlines()
    ]

    assert _write_hostile_native_types(tmp_path, _BASH) == [
        f"kind:OTHER native_type:{encoded}\n" for encoded in encoded_by_jq
    ]


def test_a_native_type_cannot_spell_another_token(tmp_path: Path) -> None:
    written_at = datetime.now(timezone.utc)

    _run_lib(tmp_path, "mngr_background_task_write task-abc OTHER 'x seen:1 since:1 kind:SHELL y'")

    records = _read_records(tmp_path)

    assert [(record.kind, record.native_type, record.started_at) for record in records] == [
        (BackgroundTaskKind.OTHER, "x seen:1 since:1 kind:SHELL y", None)
    ]
    assert records[0].last_seen_at is not None
    assert abs(records[0].last_seen_at - written_at) <= _CLOCK_SLACK


@pytest.mark.parametrize("shell", (_BASH, ("dash",)), ids="_".join)
def test_a_writer_without_od_writes_the_record_without_its_native_type(shell: Sequence[str], tmp_path: Path) -> None:
    _skip_unless_installed(shell)
    programs_dir = tmp_path / "programs"
    programs_dir.mkdir()
    for program in _PROGRAMS_OTHER_THAN_OD:
        program_path = shutil.which(program)
        assert program_path is not None
        (programs_dir / program).symlink_to(program_path)

    result = _run_lib(
        tmp_path,
        f"PATH={shlex.quote(str(programs_dir))}\n"
        "set -eu\n"
        "mngr_background_task_write task-abc SUBAGENT 'cloud session'\n"
        "mngr_background_task_write task-def SHELL shell\n"
        "echo SURVIVED",
        shell,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "SURVIVED\n", "")
    records = _read_records(tmp_path)
    assert [(record.name, record.kind, record.native_type) for record in records] == [
        ("task-abc", BackgroundTaskKind.SUBAGENT, ""),
        ("task-def", BackgroundTaskKind.SHELL, "shell"),
    ]
    assert all(record.last_seen_at is not None for record in records)


@pytest.mark.parametrize("shell", _POSIX_SHELLS, ids="_".join)
def test_the_library_works_under_a_posix_shell(shell: Sequence[str], tmp_path: Path) -> None:
    _skip_unless_installed(shell)

    result = _run_lib(
        tmp_path,
        "mngr_background_task_write task-abc SUBAGENT 'cloud session'\n"
        "mngr_background_task_write watch-xyz\n"
        "if mngr_background_task_any_recent; then echo RECENT; else echo IDLE; fi\n"
        "mngr_background_task_clear watch-",
        shell,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "RECENT\n", "")
    assert [(record.name, record.kind, record.native_type) for record in _read_records(tmp_path)] == [
        ("task-abc", BackgroundTaskKind.SUBAGENT, "cloud session")
    ]


def test_a_name_is_reduced_to_the_grammar_the_reader_accepts(tmp_path: Path) -> None:
    _run_lib(tmp_path, "mngr_background_task_write 'task-a/b c\td'")

    records = _read_records(tmp_path)

    assert len(records) == 1
    assert records[0].name == "task-a_b_c_d"


def test_writing_leaves_no_staging_file_a_reader_would_count(tmp_path: Path) -> None:
    """The staging name is a dotfile because readers collect records with ``background_tasks/*``."""
    _run_lib(tmp_path, "mngr_background_task_write task-abc")

    assert [path.name for path in (tmp_path / BACKGROUND_TASK_DIR_NAME).iterdir()] == ["task-abc"]


# Records the path each writer renames from, then holds that writer until both have staged.
_STAGING_RECORDER: Final[str] = """mv() {
    printf '%s\\n' "$2" >> "$STAGED_PATHS_LOG"
    polls=0
    while [ $(wc -l < "$STAGED_PATHS_LOG") -lt 2 ] && [ "$polls" -lt 400 ]; do
        sleep 0.05 2>/dev/null || sleep 1
        polls=$((polls + 1))
    done
    command mv "$@"
}
"""


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
def test_two_subshells_of_one_shell_stage_one_record_under_different_dotfiles(
    shell: Sequence[str], tmp_path: Path
) -> None:
    _skip_unless_installed(shell)
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    staged_paths_log = tmp_path / "staged_paths"

    result = _run_lib(
        tmp_path,
        f"STAGED_PATHS_LOG={shlex.quote(str(staged_paths_log))}\n"
        f"{_STAGING_RECORDER}"
        "(mngr_background_task_write task-abc SHELL shell) &\n"
        "(mngr_background_task_write task-abc SHELL shell) &\n"
        "wait",
        shell,
    )

    assert (result.returncode, result.stderr) == (0, "")
    first_staged, second_staged = [Path(line) for line in staged_paths_log.read_text().splitlines()]
    assert first_staged != second_staged
    assert (first_staged.parent, second_staged.parent) == (record_dir, record_dir)
    assert first_staged.name.startswith(".") and second_staged.name.startswith(".")
    assert [path.name for path in record_dir.iterdir()] == ["task-abc"]
    assert [(record.kind, record.native_type) for record in _read_records(tmp_path)] == [
        (BackgroundTaskKind.SHELL, "shell")
    ]


def test_a_writer_stages_past_a_dotfile_another_writer_left_behind(tmp_path: Path) -> None:
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir()

    result = _run_lib(
        tmp_path,
        f": > {shlex.quote(str(record_dir))}/.task-abc.$$.0.tmp\n"
        "mngr_background_task_write task-abc SHELL shell\n"
        'echo "$$"',
    )

    assert (result.returncode, result.stderr) == (0, "")
    left_behind = f".task-abc.{result.stdout.strip()}.0.tmp"
    assert sorted(path.name for path in record_dir.iterdir()) == [left_behind, "task-abc"]
    assert [(record.kind, record.native_type) for record in _read_records(tmp_path)] == [
        (BackgroundTaskKind.SHELL, "shell")
    ]


def _is_any_background_task_recent(
    state_dir: Path, shell: Sequence[str] = _BASH, heartbeat_window_override: str | None = None
) -> bool:
    """The library's verdict, read from a branch so the probe itself always exits cleanly."""
    result = _run_lib(
        state_dir,
        "if mngr_background_task_any_recent; then echo RECENT; else echo IDLE; fi",
        shell,
        heartbeat_window_override=heartbeat_window_override,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in ("RECENT", "IDLE"), result.stdout
    return result.stdout.strip() == "RECENT"


def _write_record_seen(state_dir: Path, seconds_ago: int) -> None:
    record_dir = state_dir / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir(exist_ok=True)
    seen_at = int(datetime.now(timezone.utc).timestamp()) - seconds_ago
    (record_dir / "task-abc").write_text(f"seen:{seen_at} kind:SHELL native_type:shell\n")


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
@pytest.mark.parametrize(
    ("seconds_since_seen", "heartbeat_window_override", "is_recent"),
    [
        pytest.param(0, None, True, id="just_seen"),
        pytest.param(DEFAULT_BACKGROUND_TASK_KEEPALIVE_SECONDS - 300, None, True, id="inside_the_default_window"),
        pytest.param(DEFAULT_BACKGROUND_TASK_KEEPALIVE_SECONDS + 300, None, False, id="past_the_default_window"),
        pytest.param(300, "600", True, id="inside_the_agents_own_window"),
        pytest.param(900, "600", False, id="past_the_agents_own_window"),
    ],
)
def test_a_record_keeps_the_activity_clock_fresh_for_the_window_after_it_was_seen(
    shell: Sequence[str],
    seconds_since_seen: int,
    heartbeat_window_override: str | None,
    is_recent: bool,
    tmp_path: Path,
) -> None:
    _skip_unless_installed(shell)
    _write_record_seen(tmp_path, seconds_since_seen)

    assert _is_any_background_task_recent(tmp_path, shell, heartbeat_window_override) is is_recent


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
@pytest.mark.parametrize(
    ("directory_name", "is_recent"),
    [
        pytest.param("background_tasks", True, id="background_tasks"),
        pytest.param("leases", False, id="leases"),
    ],
)
def test_the_heartbeat_counts_the_records_under_background_tasks_and_no_other_directory(
    shell: Sequence[str], directory_name: str, is_recent: bool, tmp_path: Path
) -> None:
    _skip_unless_installed(shell)
    (tmp_path / directory_name).mkdir()
    seen_at = int(datetime.now(timezone.utc).timestamp())
    (tmp_path / directory_name / "task-abc").write_text(f"seen:{seen_at} kind:SHELL native_type:shell\n")

    assert _is_any_background_task_recent(tmp_path, shell) is is_recent


@pytest.mark.parametrize("override", ["", " ", "0", "007", "4h", "-600", "1.5"])
def test_a_window_that_is_not_a_positive_count_of_seconds_is_the_default_window(tmp_path: Path, override: str) -> None:
    _write_record_seen(tmp_path, 3600)

    assert _is_any_background_task_recent(tmp_path, heartbeat_window_override=override) is True


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
def test_a_record_that_does_not_say_when_it_was_seen_does_not_keep_the_clock_fresh(
    shell: Sequence[str], tmp_path: Path
) -> None:
    _skip_unless_installed(shell)
    now = int(datetime.now(timezone.utc).timestamp())
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-empty").write_text("")
    (record_dir / "task-unknown-field").write_text("pid:4821\n")
    (record_dir / "task-started-only").write_text(f"kind:SHELL since:{now}\n")
    (record_dir / "task-seen-is-no-number").write_text("seen:soon\n")
    (record_dir / "task-seen-first-given-wins").write_text(f"seen:1 seen:{now}\n")
    (record_dir / "task-seen-inside-a-value").write_text(f"native_type:seen%3A{now}\n")
    (record_dir / "task-seen-on-a-later-line").write_text(f"kind:SHELL\nseen:{now}\n")

    assert _is_any_background_task_recent(tmp_path, shell) is False


@pytest.mark.parametrize("shell", _ALL_SHELLS, ids="_".join)
@pytest.mark.parametrize(
    ("seen_format", "is_read", "is_recent"),
    [
        pytest.param("{now:012d}", True, True, id="twelve_digits"),
        pytest.param("{now:013d}", False, False, id="thirteen_digits"),
        pytest.param("9" * 5000, False, False, id="five_thousand_digits"),
        pytest.param("{now_in_arabic_indic_digits}", False, False, id="digits_outside_ascii"),
        pytest.param("{now}x", False, False, id="a_letter_after_the_digits"),
        pytest.param("{now}\tkind:SHELL", False, False, id="a_tab_before_the_next_token"),
        pytest.param("{now}\rx", False, False, id="a_carriage_return_inside_the_line"),
        pytest.param("{now}\r", True, True, id="a_carriage_return_before_the_newline"),
        pytest.param("{later}", True, False, id="later_than_now"),
    ],
)
def test_a_seen_keeps_the_clock_fresh_only_where_the_reader_takes_it_and_it_is_not_later_than_now(
    shell: Sequence[str], seen_format: str, is_read: bool, is_recent: bool, tmp_path: Path
) -> None:
    _skip_unless_installed(shell)
    now = int(datetime.now(timezone.utc).timestamp())
    seen = seen_format.format(
        now=now, later=now + 3600, now_in_arabic_indic_digits=str(now).translate(_ARABIC_INDIC_DIGITS)
    )
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-abc").write_text(f"seen:{seen}\n")

    assert [record.last_seen_at is not None for record in _read_records(tmp_path)] == [is_read]
    assert _is_any_background_task_recent(tmp_path, shell) is is_recent


def test_one_fresh_record_is_enough_whatever_else_the_dir_holds(tmp_path: Path) -> None:
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-unknown-field").write_text("pid:4821\n")
    (record_dir / "task-no-token").write_text("forever\n")
    (record_dir / "task-unclearable").mkdir()
    (record_dir / "zz-someone-elses").write_text(f"since:1 seen:{int(datetime.now(timezone.utc).timestamp())}\n")

    assert _is_any_background_task_recent(tmp_path) is True


def test_no_record_reads_as_not_recent(tmp_path: Path) -> None:
    assert _is_any_background_task_recent(tmp_path) is False

    (tmp_path / BACKGROUND_TASK_DIR_NAME).mkdir()

    assert _is_any_background_task_recent(tmp_path) is False


def test_clearing_removes_only_the_named_prefixes(tmp_path: Path) -> None:
    """The record dir belongs to the agent, so a writer clears only what it wrote."""
    _run_lib(
        tmp_path,
        "mngr_background_task_write task-abc\n"
        "mngr_background_task_write watch-xyz\n"
        "mngr_background_task_write other-build\n"
        "mngr_background_task_clear task- watch-",
    )

    assert [record.name for record in _read_records(tmp_path)] == ["other-build"]


def test_clearing_a_record_dir_that_was_never_created_is_not_an_error(tmp_path: Path) -> None:
    result = _run_lib(tmp_path, "mngr_background_task_clear task-")

    assert result.returncode == 0, result.stderr
    assert _read_records(tmp_path) == ()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("mngr_background_task_write task-abc", id="write"),
        pytest.param("mngr_background_task_clear task-", id="clear"),
        pytest.param("mngr_background_task_dir", id="dir"),
    ],
)
def test_every_entry_point_is_a_no_op_without_an_agent_to_record_against(body: str) -> None:
    """A hook run outside an mngr agent must not fail on account of the records."""
    result = _run_lib(None, body)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_writing_an_unnamed_record_is_a_no_op(tmp_path: Path) -> None:
    """A record nothing names could never be matched to the work it stands for."""
    result = _run_lib(tmp_path, "mngr_background_task_write ''")

    assert result.returncode == 0, result.stderr
    assert _read_records(tmp_path) == ()


def test_a_name_that_starts_with_a_dot_is_not_written(tmp_path: Path) -> None:
    result = _run_lib(tmp_path, "mngr_background_task_write .task-abc SHELL shell")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / BACKGROUND_TASK_DIR_NAME).exists()


def test_the_library_leaves_strict_mode_to_the_script_that_sources_it(tmp_path: Path) -> None:
    """Sourcing the library and writing a record change none of the sourcing script's shell options."""
    before = subprocess.run(["bash", "-c", "set +e; echo $-"], capture_output=True, text=True, timeout=30)
    after = _run_lib(tmp_path, "mngr_background_task_write task-abc SHELL shell\necho $-")

    assert "e" not in before.stdout.strip()
    assert "e" not in after.stdout.strip()
    assert "u" not in after.stdout.strip()
    assert "C" not in after.stdout.strip()
