import os
import shlex
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.agents.tui_utils import EnterRetryGate
from imbue.mngr.agents.tui_utils import _build_retry_gate_check
from imbue.mngr_grok.grok_screen import GROK_ENTER_RETRY_CONDITION
from imbue.mngr_grok.grok_screen import GrokGateScreen
from imbue.mngr_grok.grok_screen import build_queued_message_count_command
from imbue.mngr_grok.grok_screen import composer_holds_text
from imbue.mngr_grok.grok_screen import count_queued_rows_matching_message
from imbue.mngr_grok.grok_screen import detect_gate_screen
from imbue.mngr_grok.grok_screen import has_cancel_hint
from imbue.mngr_grok.grok_screen import has_press_again_hint
from imbue.mngr_grok.grok_screen import has_send_now_hint
from imbue.mngr_grok.grok_screen import is_always_approve_option_selected
from imbue.mngr_grok.grok_screen import is_idle_and_empty
from imbue.mngr_grok.grok_screen import is_idle_holding_text
from imbue.mngr_grok.grok_screen import is_ready_for_paste
from imbue.mngr_grok.grok_screen import parse_queued_rows
from imbue.mngr_grok.grok_screen import read_composer_text
from imbue.mngr_grok.testing import PANE_FIXTURES_DIR
from imbue.mngr_grok.testing import read_pane_fixture
from imbue.mngr_grok.testing import select_always_approve_option

# Each shell and locale the shell snippets must agree with Python under; None keeps the inherited locale.
_SHELL_AND_LOCALE_CASES: Final[tuple[tuple[str, str | None], ...]] = (
    ("bash", None),
    ("bash", "C"),
    ("sh", None),
    ("sh", "C"),
)

_SHELL_TIMEOUT_SECONDS: Final[float] = 8.0


class _CaptureReading(FrozenModel):
    """What the pane predicates read from one capture."""

    file: str = Field(description="Capture file name in the fixture directory")
    ready: bool = Field(description="is_ready_for_paste")
    idle: bool = Field(description="is_idle_and_empty")
    gate: str = Field(description="detect_gate_screen, empty for none")
    text: bool = Field(description="composer_holds_text")
    cancel: bool = Field(description="has_cancel_hint")
    again: bool = Field(description="has_press_again_hint")


class _QueuedExpectation(FrozenModel):
    """The queued rows of one capture and the messages it does and does not show queued."""

    rows: tuple[str, ...] = Field(description="parse_queued_rows")
    queued: tuple[str, ...] = Field(default=(), description="Messages shown queued")
    queued_files: tuple[str, ...] = Field(default=(), description="Message files whose content is shown queued")
    not_queued: tuple[str, ...] = Field(default=(), description="Messages not shown queued")
    not_queued_files: tuple[str, ...] = Field(
        default=(), description="Message files whose content is not shown queued"
    )


class _PaneManifest(FrozenModel):
    """The expectations recorded in the fixture directory's manifest."""

    captures: tuple[_CaptureReading, ...] = Field(description="One reading per capture")
    queued: dict[str, _QueuedExpectation] = Field(description="Queued expectations by capture file name")


def _load_pane_manifest() -> _PaneManifest:
    return _PaneManifest.model_validate(
        tomllib.loads((PANE_FIXTURES_DIR / "manifest.toml").read_text(encoding="utf-8"))
    )


_MANIFEST: Final[_PaneManifest] = _load_pane_manifest()


def _read_capture_reading(file_name: str) -> _CaptureReading:
    pane = read_pane_fixture(file_name)
    gate = detect_gate_screen(pane)
    return _CaptureReading(
        file=file_name,
        ready=is_ready_for_paste(pane),
        idle=is_idle_and_empty(pane),
        gate="" if gate is None else str(gate),
        text=composer_holds_text(pane),
        cancel=has_cancel_hint(pane),
        again=has_press_again_hint(pane),
    )


def _get_messages(names: tuple[str, ...], file_names: tuple[str, ...]) -> tuple[str, ...]:
    return names + tuple((PANE_FIXTURES_DIR / file_name).read_text(encoding="utf-8") for file_name in file_names)


def _run_shell_script(shell: str, locale: str | None, script: str, tmp_path: Path) -> list[str]:
    script_path = tmp_path / "script.sh"
    script_path.write_text(script, encoding="utf-8")
    environment = dict(os.environ) if locale is None else {**os.environ, "LC_ALL": locale}
    result = subprocess.run(
        [shell, str(script_path)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=_SHELL_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _count_queued_rows_in_shell_as_python_does(
    shell: str, locale: str | None, panes: Sequence[str], messages: Sequence[str], tmp_path: Path
) -> list[list[int]]:
    """Each message's queued-row count on each pane, by pane, after checking the shell command counts as Python does."""
    script = "\n".join(
        f"raw_pane={shlex.quote(pane)}\n"
        + "\n".join(build_queued_message_count_command(message) for message in messages)
        for pane in panes
    )

    shell_counts = _run_shell_script(shell, locale, script, tmp_path)

    counts_by_pane = [[count_queued_rows_matching_message(pane, message) for message in messages] for pane in panes]
    assert shell_counts == [str(count) for pane_counts in counts_by_pane for count in pane_counts]
    return counts_by_pane


@pytest.mark.parametrize("expected", _MANIFEST.captures, ids=[capture.file for capture in _MANIFEST.captures])
def test_pane_predicates_read_each_capture_as_its_manifest_entry(expected: _CaptureReading) -> None:
    assert _read_capture_reading(expected.file) == expected


def test_manifest_covers_every_capture_in_the_fixture_directory() -> None:
    capture_files = {path.name for path in PANE_FIXTURES_DIR.glob("*.txt")}
    manifest_files = [capture.file for capture in _MANIFEST.captures]

    assert sorted(manifest_files) == sorted(capture_files)
    assert set(_MANIFEST.queued) <= capture_files


def test_manifest_covers_every_gate_screen() -> None:
    labelled_gates = {capture.gate for capture in _MANIFEST.captures}

    assert {str(gate) for gate in GrokGateScreen} <= labelled_gates


@pytest.mark.parametrize("file_name", sorted(_MANIFEST.queued))
def test_parse_queued_rows_returns_the_visible_start_of_each_queued_message(file_name: str) -> None:
    assert parse_queued_rows(read_pane_fixture(file_name)) == _MANIFEST.queued[file_name].rows


@pytest.mark.parametrize(
    "file_name", sorted(capture.file for capture in _MANIFEST.captures if capture.file not in _MANIFEST.queued)
)
def test_captures_without_queued_expectations_show_no_queued_rows(file_name: str) -> None:
    assert parse_queued_rows(read_pane_fixture(file_name)) == ()


@pytest.mark.parametrize("file_name", sorted(_MANIFEST.queued))
def test_queued_rows_match_the_queued_messages_and_no_others(file_name: str) -> None:
    expectation = _MANIFEST.queued[file_name]
    pane = read_pane_fixture(file_name)

    queued_messages = _get_messages(expectation.queued, expectation.queued_files)
    not_queued_messages = _get_messages(expectation.not_queued, expectation.not_queued_files)

    assert all(count_queued_rows_matching_message(pane, message) > 0 for message in queued_messages)
    assert [count_queued_rows_matching_message(pane, message) for message in not_queued_messages] == [0] * len(
        not_queued_messages
    )


@pytest.mark.parametrize(("shell", "locale"), _SHELL_AND_LOCALE_CASES)
def test_enter_retry_condition_agrees_with_composer_holds_text_on_every_capture(
    shell: str, locale: str | None, tmp_path: Path
) -> None:
    gate_check = _build_retry_gate_check(EnterRetryGate(condition_command=NonEmptyStr(GROK_ENTER_RETRY_CONDITION)))
    file_names = [capture.file for capture in _MANIFEST.captures]
    script = "\n".join(
        f'raw_pane="$( cat {shlex.quote(str(PANE_FIXTURES_DIR / file_name))} )"\n'
        f"if {gate_check}; then echo retry; else echo no; fi"
        for file_name in file_names
    )

    verdicts = _run_shell_script(shell, locale, script, tmp_path)

    expected = ["retry" if composer_holds_text(read_pane_fixture(file_name)) else "no" for file_name in file_names]
    assert len(verdicts) == len(file_names)
    assert dict(zip(file_names, verdicts, strict=True)) == dict(zip(file_names, expected, strict=True))
    assert expected.count("retry") > 0


def _get_every_manifest_message() -> tuple[str, ...]:
    messages = {
        message
        for expectation in _MANIFEST.queued.values()
        for message in (
            _get_messages(expectation.queued, expectation.queued_files)
            + _get_messages(expectation.not_queued, expectation.not_queued_files)
        )
    }
    return tuple(sorted(messages))


@pytest.mark.parametrize(("shell", "locale"), _SHELL_AND_LOCALE_CASES)
def test_queued_message_count_command_agrees_with_python_on_every_queued_capture(
    shell: str, locale: str | None, tmp_path: Path
) -> None:
    panes = [read_pane_fixture(file_name) for file_name in sorted(_MANIFEST.queued)]

    counts_by_pane = _count_queued_rows_in_shell_as_python_does(
        shell, locale, panes, _get_every_manifest_message(), tmp_path
    )

    assert any(count > 0 for pane_counts in counts_by_pane for count in pane_counts)


# Messages whose first line has no ASCII letters or digits, and the queued row that shows each.
_QUEUED_ROWS_WITHOUT_LETTERS_BY_MESSAGE: Final[dict[str, str]] = {
    "Исправь падающий тест": "Исправь падающий тест",
    "修复失败的测试\n然后运行": "修复失败的测试 (+1 line)",
    "```\nTraceback (most recent call last):\n  File": "``` (+2 lines)",
    "---\nrelease notes": "--- (+1 line)",
}
_ONE_QUEUED_PANE: Final[str] = "w170_g_busy_queued1.txt"
_ONE_QUEUED_ROW_TEXT: Final[str] = "typed while busy one"


def _replace_one_queued_row(row_text: str) -> str:
    return read_pane_fixture(_ONE_QUEUED_PANE).replace(f"#1 {_ONE_QUEUED_ROW_TEXT}", f"#1 {row_text}")


@pytest.mark.parametrize(("message", "row_text"), sorted(_QUEUED_ROWS_WITHOUT_LETTERS_BY_MESSAGE.items()))
def test_a_message_whose_first_line_has_no_letters_or_digits_matches_only_rows_without_any(
    message: str, row_text: str
) -> None:
    queued_pane = _replace_one_queued_row(row_text)

    assert count_queued_rows_matching_message(queued_pane, message) == 1
    assert count_queued_rows_matching_message(read_pane_fixture("w170_e_busy_empty.txt"), message) == 0
    assert count_queued_rows_matching_message(read_pane_fixture(_ONE_QUEUED_PANE), message) == 0
    assert count_queued_rows_matching_message(queued_pane, _ONE_QUEUED_ROW_TEXT) == 0


@pytest.mark.parametrize(("shell", "locale"), _SHELL_AND_LOCALE_CASES)
def test_queued_message_count_command_agrees_with_python_on_rows_without_letters_or_digits(
    shell: str, locale: str | None, tmp_path: Path
) -> None:
    panes = [read_pane_fixture(_ONE_QUEUED_PANE)] + [
        _replace_one_queued_row(row_text) for row_text in _QUEUED_ROWS_WITHOUT_LETTERS_BY_MESSAGE.values()
    ]
    messages = [*_QUEUED_ROWS_WITHOUT_LETTERS_BY_MESSAGE, _ONE_QUEUED_ROW_TEXT]

    counts_by_pane = _count_queued_rows_in_shell_as_python_does(shell, locale, panes, messages, tmp_path)

    assert {0, 1} <= {count for pane_counts in counts_by_pane for count in pane_counts}


def _replace_blank_transcript_rows(pane: str, transcript_rows: tuple[str, ...]) -> str:
    """The pane with its first blank rows below the header replaced by the given rows."""
    lines = pane.split("\n")
    blank_idxs = [idx for idx, line in enumerate(lines) if idx > 2 and line.strip() == ""][: len(transcript_rows)]
    replaced = dict(zip(blank_idxs, transcript_rows, strict=True))
    return "\n".join(replaced.get(idx, line) for idx, line in enumerate(lines))


def _insert_row_above_composer(pane: str, row: str) -> str:
    """The pane with the row put on the blank line above the composer box's top border."""
    lines = pane.split("\n")
    top_border_idx = max(idx for idx, line in enumerate(lines) if line.lstrip().startswith("╭"))
    return "\n".join(row if idx == top_border_idx - 1 else line for idx, line in enumerate(lines))


def test_gate_screen_text_in_the_transcript_is_not_a_gate_screen() -> None:
    pane = _replace_blank_transcript_rows(
        read_pane_fixture("w170_c_idle_after_turn.txt"),
        (
            "     Do you trust the contents of this directory?",
            "     Login with Grok  l",
            "     Quit  q",
            "     Tier: Free  [Refresh]",
            "     Logout  ctrl+l",
        ),
    )

    assert detect_gate_screen(pane) is None
    assert is_ready_for_paste(pane)
    assert is_idle_and_empty(pane)


def test_a_quit_row_beside_the_composer_is_not_a_consent_notice() -> None:
    consent_pane = read_pane_fixture("gates_consent_notice_default_title_and_label.txt")
    pane = _replace_blank_transcript_rows(consent_pane, ("  │ ❯ draft reply   │",))

    assert detect_gate_screen(consent_pane) is GrokGateScreen.CONSENT
    assert detect_gate_screen(pane) is None


def test_numbered_line_in_the_transcript_is_not_a_queued_row() -> None:
    pane = _replace_blank_transcript_rows(
        read_pane_fixture("w170_c_idle_after_turn.txt"), ("     #1 fix the flaky test first",)
    )

    assert parse_queued_rows(pane) == ()
    assert count_queued_rows_matching_message(pane, "fix the flaky test first") == 0
    assert is_idle_and_empty(pane)


def test_spinner_text_in_the_transcript_does_not_make_the_pane_busy() -> None:
    pane = _replace_blank_transcript_rows(
        read_pane_fixture("w170_c_idle_after_turn.txt"), ("     ⠋ Starting session… 0.0s", "     ⠴ Responding… 2.9s")
    )

    assert is_ready_for_paste(pane)
    assert is_idle_and_empty(pane)


def test_queued_row_alone_keeps_the_pane_from_reading_idle() -> None:
    pane = _insert_row_above_composer(
        read_pane_fixture("w170_c_idle_after_turn.txt"), f"    #1 {_ONE_QUEUED_ROW_TEXT}"
    )

    assert parse_queued_rows(pane) == (_ONE_QUEUED_ROW_TEXT,)
    assert is_ready_for_paste(pane)
    assert not is_idle_and_empty(pane)


def test_always_approve_option_reads_selected_only_where_the_card_marks_it() -> None:
    permission_card = read_pane_fixture("perm_card.txt")

    assert not any(
        is_always_approve_option_selected(read_pane_fixture(capture.file)) for capture in _MANIFEST.captures
    )
    assert is_always_approve_option_selected(select_always_approve_option(permission_card))


@pytest.mark.parametrize(
    ("file_name", "is_send_now"),
    (
        ("w170_g_busy_queued1.txt", True),
        ("w80_g_busy_queued2.txt", True),
        ("p170_queued_paste_six.txt", True),
        ("w170_f_busy_typed.txt", False),
        ("w170_g_busy_queued1_typed.txt", False),
        ("w170_e_busy_empty.txt", False),
        ("w170_c_idle_after_turn.txt", False),
        ("perm_card.txt", False),
    ),
)
def test_has_send_now_hint_reads_the_footer_of_a_busy_empty_composer_with_a_message_queued(
    file_name: str, is_send_now: bool
) -> None:
    assert has_send_now_hint(read_pane_fixture(file_name)) is is_send_now


def test_spinner_row_alone_keeps_the_pane_from_reading_idle() -> None:
    pane = _insert_row_above_composer(read_pane_fixture("w170_c_idle_after_turn.txt"), "    ⠴ Responding… 2.9s")

    assert is_ready_for_paste(pane)
    assert not has_cancel_hint(pane)
    assert not is_idle_and_empty(pane)


@pytest.mark.parametrize(
    "pane",
    (
        pytest.param("", id="empty"),
        pytest.param("\n   \n\t\n", id="blank"),
        pytest.param("  ╭──────────────╮\n  │ tool output  │\n  ╰──────────────╯\n", id="box_without_prompt_row"),
        pytest.param(
            "  │ tool output  │\n  ╰──────────────╯\n\n  Shift+Tab:mode  │  Ctrl+x:shortcuts\n",
            id="box_rows_up_to_the_top",
        ),
    ),
)
def test_pane_without_a_composer_box_is_not_ready_and_shows_no_gate_screen(pane: str) -> None:
    assert not is_ready_for_paste(pane)
    assert not is_idle_and_empty(pane)
    assert detect_gate_screen(pane) is None


def test_queued_row_that_normalizes_to_nothing_matches_no_message_with_letters_or_digits() -> None:
    pane = read_pane_fixture("int_5d_busy_queued.txt").replace("    #1 queued during 5d", "    #1 …")

    assert parse_queued_rows(pane) == ("",)
    assert count_queued_rows_matching_message(pane, "queued during 5d") == 0


@pytest.mark.parametrize(
    ("pane_file_name", "expected_text"),
    [
        ("w170_b_fresh_typed.txt", "hello typed text"),
        ("p170_paste_four_line_paste_composer.txt", "[Pasted: 4 lines]"),
        ("w170_c_idle_after_turn.txt", ""),
        (
            "p170_keys_five_line_under_1024_composer.txt",
            "\n".join(f"KEYS5 line {number} m{number}{number} short text for send-keys" for number in range(1, 6)),
        ),
        (
            "drop_restored_five_lines.txt",
            "\n".join(f"HOLDTEST line {number} of the held prompt 86a5065a" for number in range(1, 6)),
        ),
    ],
)
def test_read_composer_text_reads_every_composer_row(pane_file_name: str, expected_text: str) -> None:
    assert read_composer_text(read_pane_fixture(pane_file_name)) == expected_text


@pytest.mark.parametrize(
    ("pane_file_name", "is_holding_text"),
    [
        ("drop_restored_one_line.txt", True),
        ("drop_restored_five_lines.txt", True),
        ("drop_restored_pasted_six_lines.txt", True),
        ("w170_d_idle_typed.txt", True),
        ("drop_after_clearing_ctrlc.txt", False),
        ("w170_c_idle_after_turn.txt", False),
        ("w170_f_busy_typed.txt", False),
        ("w170_g_busy_queued1_typed.txt", False),
        ("int_5c_idle_ctrlc_01.txt", False),
        ("perm_after_ctrlc.txt", False),
        ("perm_card_after_typing.txt", False),
    ],
)
def test_is_idle_holding_text_reads_an_idle_composer_with_text(pane_file_name: str, is_holding_text: bool) -> None:
    assert is_idle_holding_text(read_pane_fixture(pane_file_name)) is is_holding_text


def test_read_composer_text_without_a_composer_is_empty() -> None:
    assert read_composer_text("no box here\n") == ""


def test_message_without_letters_or_digits_in_its_first_line_is_not_matched_by_its_later_lines() -> None:
    pane = read_pane_fixture("int_5d_busy_queued.txt")

    assert count_queued_rows_matching_message(pane, "!!! ...\nqueued during 5d") == 0
    assert count_queued_rows_matching_message(pane, "") == 0


@pytest.mark.parametrize(("shell", "locale"), _SHELL_AND_LOCALE_CASES)
def test_queued_message_count_command_agrees_with_python_on_non_ascii_rows(
    shell: str, locale: str | None, tmp_path: Path
) -> None:
    messages = ("İstanbul trip plan", "Größe ÄÖÜ 名前 café №1", "cool it to 20 K first")
    idle_pane = read_pane_fixture("w170_c_idle_after_turn.txt")
    panes = [_insert_row_above_composer(idle_pane, f"    #1 {message}") for message in messages]

    counts_by_pane = _count_queued_rows_in_shell_as_python_does(shell, locale, panes, messages, tmp_path)

    assert [counts_by_pane[pane_idx][pane_idx] for pane_idx in range(len(panes))] == [1] * len(messages)
