import re
import shlex
from collections.abc import Sequence
from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure


class GrokGateScreen(UpperCaseStrEnum):
    """A grok screen that holds the keyboard in place of the session composer, so any key typed into it acts on it."""

    TRUST_DIALOG = auto()
    LOGIN = auto()
    CONSENT = auto()
    PAYWALL = auto()
    PERMISSION_CARD = auto()


# Footer hints are joined by two spaces, a bar and two spaces: `Shift+Tab:mode  │  Ctrl+x:shortcuts`.
_FOOTER_HINT_SEPARATOR: Final[str] = "  │"
# One footer hint: a key without spaces, a colon, a label (`Shift+Tab:mode`, `Enter:send now`, `1/4:select`).
_KEY_HINT_RE: Final[re.Pattern[str]] = re.compile(r"^\S+:\S")
# Part of the hint of a pending double-press confirmation: `Ctrl+c:press again to quit`.
_PRESS_AGAIN_HINT_MARKER: Final[str] = " again to "
_CANCEL_HINT_SUFFIX: Final[str] = ":cancel"
# The first footer hint while the idle composer holds typed, pasted or restored text.
_IDLE_COMPOSER_TEXT_FIRST_HINT: Final[str] = "Enter:send"
# The first footer hint while the composer holds text, idle and busy.
_COMPOSER_TEXT_FIRST_HINTS: Final[tuple[str, ...]] = (_IDLE_COMPOSER_TEXT_FIRST_HINT, "Enter:queue")
_SUBMIT_FIRST_HINT_PREFIX: Final[str] = "Enter:"
# The first footer hint while a turn runs with the composer empty and a message queued.
_SEND_NOW_FIRST_HINT: Final[str] = "Enter:send now"

# The composer's prompt row: `  │ ❯ typed text   │`.
_COMPOSER_PROMPT_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*│ ❯")
# Any row inside a box: `  │  second line of input   │`.
_BOX_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*│")
_BOX_SIDE: Final[str] = "│"
# A box's bottom border: `  ╰────── grok-4.6 ─╯`.
_BOX_BOTTOM_BORDER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*╰─.*╯\s*$")

# The indent of the rows between the transcript and the composer; transcript rows are indented one column more.
_LIVE_ROW_INDENT: Final[str] = "    "
# A spinner row: `    ⠴ Responding… 2.9s`.
_SPINNER_ROW_RE: Final[re.Pattern[str]] = re.compile(rf"^{_LIVE_ROW_INDENT}[⠁-⣿] [^…]+…")
# The spinner row of a `-s` launch before its session exists: `    ⠋ Starting session… 0.0s`.
_STARTING_SESSION_ROW_RE: Final[re.Pattern[str]] = re.compile(rf"^{_LIVE_ROW_INDENT}[⠁-⣿] Starting session…")
# The row a `--resume` launch shows before it replays the transcript.
_LOADING_SESSION_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*Loading session [0-9a-f-]{36}\.\.\.\s*$")
# A queued follow-up: `    #2 MULTIQ first line (+2 lines)`.
_QUEUED_ROW_RE: Final[re.Pattern[str]] = re.compile(rf"^{_LIVE_ROW_INDENT}#[0-9]+ (?P<text>[^ \t].*)$")
# The suffix of a queued row whose message has more lines: ` (+2 lines)`.
_QUEUED_ROW_MORE_LINES_RE: Final[re.Pattern[str]] = re.compile(r" \(\+[0-9]+ lines?\)$")
_QUEUED_ROW_TRUNCATION_MARK: Final[str] = "…"
# How many normalized characters of a queued row are compared with the message's first line.
_QUEUED_ROW_MATCH_MAX_LENGTH: Final[int] = 400

# Dropped before lowercasing, as `LC_ALL=C awk` does to a queued row.
_NON_ALPHANUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]")

# The version badge that ends every gate screen's last line: `Grok Build  1.0.62`.
_VERSION_BADGE_RE: Final[re.Pattern[str]] = re.compile(r"^Grok Build\s+[0-9]+\.[0-9]+\.[0-9]+\S*$")
_TRUST_DIALOG_QUESTION: Final[str] = "Do you trust the contents of this directory?"
_LOGIN_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*Login with Grok\s{2,}l\s*$")
_CONSENT_QUIT_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*Quit\s{2,}q\s*$")
_PAYWALL_TIER_LABEL: Final[str] = "Tier: "
_PAYWALL_REFRESH_LABEL: Final[str] = "[Refresh]"
_PAYWALL_LOGOUT_ROW_RE: Final[re.Pattern[str]] = re.compile(r"Logout\s{2,}ctrl\+l")
# The first footer hint of a permission card that has the focus: `1/4:select`.
_PERMISSION_SELECT_HINT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]+/[0-9]+:select$")
# The footer hint of a permission card while the scrollback has the focus.
_PERMISSION_FOCUS_HINT: Final[str] = "Tab/Space:permission"
# A permission card's always-approve option while it is the selected one: `  ┃  1 (●) Yes, and don't ask again …`.
_SELECTED_ALWAYS_APPROVE_OPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*┃\s+[0-9]+ \(●\) .*\(always-approve mode\)\s*$"
)

_COMPOSER_TEXT_FIRST_HINT_CASE_PATTERNS: Final[str] = " | ".join(
    "'" + hint + "'" for hint in _COMPOSER_TEXT_FIRST_HINTS
)

# Shell condition for an EnterRetryGate: exits 0 only when the footer's first hint is one of the composer-text hints.
GROK_ENTER_RETRY_CONDITION: Final[str] = (
    "footer=\"$(printf '%s\\n' \"$raw_pane\" | awk 'NF { last = $0 } END { print last }')\"\n"
    f"first=\"$(printf '%s\\n' \"$footer\" | sed -e 's/^ *//' -e 's/{_FOOTER_HINT_SEPARATOR}.*$//' -e 's/ *$//')\"\n"
    'case "$first" in\n'
    f"  {_COMPOSER_TEXT_FIRST_HINT_CASE_PATTERNS}) exit 0 ;;\n"
    "esac\n"
    "exit 1"
)

# Awk program printing count_queued_rows_matching_message for the pane on stdin and the message's `target`.
_QUEUED_ROW_COUNT_AWK_PROGRAM: Final[str] = (
    rf"/^{_LIVE_ROW_INDENT}#[0-9]+ [^ \t]/ {{ "
    rf'row = $0; sub(/^{_LIVE_ROW_INDENT}#[0-9]+ /, "", row); sub(/[ \t]+$/, "", row); '
    rf'sub(/{_QUEUED_ROW_MORE_LINES_RE.pattern}/, "", row); '
    rf'gsub(/{_NON_ALPHANUMERIC_RE.pattern}/, "", row); row = tolower(row); '
    rf"row = substr(row, 1, {_QUEUED_ROW_MATCH_MAX_LENGTH}); "
    r'if ((target == "" && row == "") || (row != "" && index(target, row) == 1)) count++ } '
    r"END { print count + 0 }"
)


class _ComposerBottom(FrozenModel):
    """The composer box at the bottom of a session pane, with the footer line under it if there is one."""

    footer_line: str | None = Field(
        description="The footer line under the composer box; None when the box is the pane's last block"
    )


@pure
def _split_pane_lines(pane: str) -> list[str]:
    return pane.split("\n")


@pure
def _is_blank_line(line: str) -> bool:
    return line.strip(" \t") == ""


@pure
def _find_last_non_blank_idx(lines: Sequence[str], before_idx: int) -> int | None:
    for idx in range(before_idx - 1, -1, -1):
        if not _is_blank_line(lines[idx]):
            return idx
    return None


@pure
def _split_footer_hints(footer_line: str) -> tuple[str, ...]:
    """The hints of a footer line, the first cut exactly as GROK_ENTER_RETRY_CONDITION cuts it.

    `  Enter:send  │  Opt+Enter:newline  │` -> ("Enter:send", "Opt+Enter:newline")
    """
    segments = footer_line.lstrip(" ").split(_FOOTER_HINT_SEPARATOR)
    return tuple(segment.strip(" ") for segment in segments if segment.strip(" ") != "")


@pure
def _get_last_line_hints(lines: Sequence[str]) -> tuple[str, ...]:
    last_idx = _find_last_non_blank_idx(lines, len(lines))
    return () if last_idx is None else _split_footer_hints(lines[last_idx])


@pure
def _is_composer_bottom_border(lines: Sequence[str], border_idx: int) -> bool:
    """Whether the line closes a box whose rows, read upwards, reach the composer's prompt row."""
    if _BOX_BOTTOM_BORDER_RE.match(lines[border_idx]) is None:
        return False
    for row_idx in range(border_idx - 1, -1, -1):
        if _COMPOSER_PROMPT_ROW_RE.match(lines[row_idx]) is not None:
            return True
        if _BOX_ROW_RE.match(lines[row_idx]) is None:
            return False
    return False


@pure
def _find_composer_bottom(lines: Sequence[str]) -> _ComposerBottom | None:
    """The composer box as the pane's last block, or directly above its last line; None when it is neither."""
    last_idx = _find_last_non_blank_idx(lines, len(lines))
    if last_idx is None:
        return None
    if _is_composer_bottom_border(lines, last_idx):
        return _ComposerBottom(footer_line=None)
    border_idx = _find_last_non_blank_idx(lines, last_idx)
    if border_idx is not None and _is_composer_bottom_border(lines, border_idx):
        return _ComposerBottom(footer_line=lines[last_idx])
    return None


@pure
def _is_key_hint_footer(footer_line: str) -> bool:
    """Whether the line is a footer of key hints and not a pending double-press confirmation."""
    hints = _split_footer_hints(footer_line)
    if len(hints) == 0 or _KEY_HINT_RE.match(hints[0]) is None:
        return False
    return not any(_PRESS_AGAIN_HINT_MARKER in hint for hint in hints)


@pure
def _has_startup_row(lines: Sequence[str]) -> bool:
    return any(
        _STARTING_SESSION_ROW_RE.match(line) is not None or _LOADING_SESSION_ROW_RE.match(line) is not None
        for line in lines
    )


@pure
def _detect_gate_screen_in_lines(lines: Sequence[str]) -> GrokGateScreen | None:
    last_line_hints = _get_last_line_hints(lines)
    if len(last_line_hints) == 0:
        return None
    if _PERMISSION_SELECT_HINT_RE.match(last_line_hints[0]) is not None or _PERMISSION_FOCUS_HINT in last_line_hints:
        return GrokGateScreen.PERMISSION_CARD
    if _VERSION_BADGE_RE.match(last_line_hints[-1]) is None:
        return None
    if any(line.strip() == _TRUST_DIALOG_QUESTION for line in lines):
        return GrokGateScreen.TRUST_DIALOG
    elif any(_LOGIN_ROW_RE.match(line) is not None for line in lines):
        return GrokGateScreen.LOGIN
    elif any(_PAYWALL_TIER_LABEL in line and _PAYWALL_REFRESH_LABEL in line for line in lines) and any(
        _PAYWALL_LOGOUT_ROW_RE.search(line) is not None for line in lines
    ):
        return GrokGateScreen.PAYWALL
    elif any(_CONSENT_QUIT_ROW_RE.match(line) is not None for line in lines) and not any(
        _COMPOSER_PROMPT_ROW_RE.match(line) is not None for line in lines
    ):
        return GrokGateScreen.CONSENT
    else:
        return None


@pure
def detect_gate_screen(pane: str) -> GrokGateScreen | None:
    """The gate screen the pane shows, or None.

    The trust dialog, login, consent and paywall screens all end with the version badge; the
    permission card is told by its footer.
    """
    return _detect_gate_screen_in_lines(_split_pane_lines(pane))


@pure
def is_always_approve_option_selected(pane: str) -> bool:
    """Whether a permission card's selected option is the always-approve one, so Enter would turn that mode on."""
    return any(_SELECTED_ALWAYS_APPROVE_OPTION_RE.match(line) is not None for line in _split_pane_lines(pane))


@pure
def _get_queued_row_text(row_text_after_number: str) -> str:
    without_trailing_space = row_text_after_number.rstrip(" \t")
    without_more_lines = _QUEUED_ROW_MORE_LINES_RE.sub("", without_trailing_space)
    return without_more_lines.removesuffix(_QUEUED_ROW_TRUNCATION_MARK).rstrip(" ")


@pure
def _parse_queued_rows_in_lines(lines: Sequence[str]) -> tuple[str, ...]:
    matches = (_QUEUED_ROW_RE.match(line) for line in lines)
    return tuple(_get_queued_row_text(match.group("text")) for match in matches if match is not None)


@pure
def parse_queued_rows(pane: str) -> tuple[str, ...]:
    """The visible start of each queued follow-up, top to bottom.

    A row shows the first line of its message cut to the pane width:
    `    #2 MULTIQ first line (+2 lines)` -> "MULTIQ first line",
    `    #1 LONGQUEUED start word1 … word21 word2…` -> "LONGQUEUED start word1 … word21 word2".
    """
    return _parse_queued_rows_in_lines(_split_pane_lines(pane))


@pure
def _find_ready_composer_bottom(lines: Sequence[str]) -> _ComposerBottom | None:
    """The composer bottom of a pane that takes a paste now, or None."""
    composer_bottom = _find_composer_bottom(lines)
    if composer_bottom is None:
        return None
    if composer_bottom.footer_line is not None and not _is_key_hint_footer(composer_bottom.footer_line):
        return None
    if _has_startup_row(lines) or _detect_gate_screen_in_lines(lines) is not None:
        return None
    return composer_bottom


@pure
def is_ready_for_paste(pane: str) -> bool:
    """Whether the session composer takes a paste now, idle or busy.

    The composer box is the pane's last block (a fresh session without a footer) or sits directly
    above a footer of key hints that is not a pending `press again to quit`, and no session is
    still starting or loading.
    """
    return _find_ready_composer_bottom(_split_pane_lines(pane)) is not None


@pure
def _read_idle_first_hint(pane: str) -> str | None:
    """The footer's first hint ("" without a footer) of a pane ready for a paste, idle and with nothing queued."""
    lines = _split_pane_lines(pane)
    composer_bottom = _find_ready_composer_bottom(lines)
    if composer_bottom is None:
        return None
    hints = () if composer_bottom.footer_line is None else _split_footer_hints(composer_bottom.footer_line)
    if any(hint.endswith(_CANCEL_HINT_SUFFIX) for hint in hints):
        return None
    if any(_SPINNER_ROW_RE.match(line) is not None for line in lines):
        return None
    if len(_parse_queued_rows_in_lines(lines)) > 0:
        return None
    return hints[0] if len(hints) > 0 else ""


@pure
def is_idle_and_empty(pane: str) -> bool:
    """Whether the pane is ready for a paste with no turn running, nothing queued and an empty composer.

    Emptiness is read from the footer's first hint, never from the composer row: the scrollback-focus
    placeholder `Build anything` prints exactly like typed text.
    """
    first_hint = _read_idle_first_hint(pane)
    return first_hint is not None and not first_hint.startswith(_SUBMIT_FIRST_HINT_PREFIX)


@pure
def is_idle_holding_text(pane: str) -> bool:
    """Whether the pane is ready for a paste with no turn running, nothing queued and text in the composer."""
    return _read_idle_first_hint(pane) == _IDLE_COMPOSER_TEXT_FIRST_HINT


@pure
def composer_holds_text(pane: str) -> bool:
    """Whether the footer's first hint says the composer holds text, read as GROK_ENTER_RETRY_CONDITION reads it."""
    hints = _get_last_line_hints(_split_pane_lines(pane))
    return len(hints) > 0 and hints[0] in _COMPOSER_TEXT_FIRST_HINTS


@pure
def _read_box_row_text(row_text_after_left_side: str) -> str:
    return row_text_after_left_side.rstrip().removesuffix(_BOX_SIDE).strip()


@pure
def read_composer_text(pane: str) -> str:
    """The composer's text, one line per row from its prompt row down.

    `  │ ❯ first line   │` and `  │   second line   │` -> "first line\\nsecond line".
    """
    lines = _split_pane_lines(pane)
    prompt_row_indices = [idx for idx, line in enumerate(lines) if _COMPOSER_PROMPT_ROW_RE.match(line) is not None]
    if not prompt_row_indices:
        return ""
    prompt_row_idx = prompt_row_indices[-1]
    row_texts = [_read_box_row_text(_COMPOSER_PROMPT_ROW_RE.sub("", lines[prompt_row_idx], count=1))]
    for line in lines[prompt_row_idx + 1 :]:
        if _BOX_ROW_RE.match(line) is None:
            break
        row_texts.append(_read_box_row_text(_BOX_ROW_RE.sub("", line, count=1)))
    return "\n".join(row_texts)


@pure
def has_cancel_hint(pane: str) -> bool:
    """Whether a footer hint ends in `:cancel`: a turn is running, or a permission card is up."""
    return any(hint.endswith(_CANCEL_HINT_SUFFIX) for hint in _get_last_line_hints(_split_pane_lines(pane)))


@pure
def has_send_now_hint(pane: str) -> bool:
    """Whether the footer's first hint is `Enter:send now`: Enter cuts the running turn short to send the queued message."""
    hints = _get_last_line_hints(_split_pane_lines(pane))
    return len(hints) > 0 and hints[0] == _SEND_NOW_FIRST_HINT


@pure
def has_press_again_hint(pane: str) -> bool:
    """Whether the footer is a pending double-press confirmation (`Ctrl+c:press again to quit`): one more press quits."""
    return any(_PRESS_AGAIN_HINT_MARKER in hint for hint in _get_last_line_hints(_split_pane_lines(pane)))


@pure
def _normalize_for_match(text: str) -> str:
    return _NON_ALPHANUMERIC_RE.sub("", text).lower()


@pure
def _build_queued_row_match_target(message: str) -> str:
    """The normalized start of the message's first line, which a queued row of it must begin."""
    first_line = message.lstrip().split("\n", 1)[0]
    return _normalize_for_match(first_line)[:_QUEUED_ROW_MATCH_MAX_LENGTH]


@pure
def _is_queued_row_matching_target(normalized_row: str, target: str) -> bool:
    if target == "":
        return normalized_row == ""
    return normalized_row != "" and target.startswith(normalized_row)


@pure
def count_queued_rows_matching_message(pane: str, message: str) -> int:
    """How many queued rows can show the message.

    A row matches when it normalizes to a non-empty prefix of the message's normalized first line, or, when that
    line has no letters or digits, when the row has none either.
    """
    target = _build_queued_row_match_target(message)
    normalized_rows = (_normalize_for_match(row)[:_QUEUED_ROW_MATCH_MAX_LENGTH] for row in parse_queued_rows(pane))
    return sum(1 for normalized_row in normalized_rows if _is_queued_row_matching_target(normalized_row, target))


@pure
def build_queued_message_count_command(message: str) -> str:
    """Shell snippet printing count_queued_rows_matching_message for the pane in `$raw_pane`."""
    target = _build_queued_row_match_target(message)
    return (
        f"printf '%s\\n' \"$raw_pane\" | LC_ALL=C awk -v target={shlex.quote(target)} "
        f"{shlex.quote(_QUEUED_ROW_COUNT_AWK_PROGRAM)}"
    )
