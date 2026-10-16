from typing import Final

# Shell-mode input rows verbatim from `capture-pane -e` captures of the claude build
# `CLAUDE_CODE_VERSION` names.
# An empty box: a bare `!` submitted on a fresh agent.
STYLED_EMPTY_SHELL_ROW_WITH_TRY_HINT: Final[str] = (
    '!\xa0\x1b[7m\x1b[39mT\x1b[0;2mry\x1b[0m \x1b[2m"how\x1b[0m \x1b[2mdo\x1b[0m \x1b[2mI\x1b[0m '
    '\x1b[2mlog\x1b[0m \x1b[2man\x1b[0m \x1b[2merror?"\x1b[0m'
)
# A human typed the hint's words.
STYLED_TYPED_SHELL_ROW_READING_LIKE_THE_HINT: Final[str] = '!\xa0\x1b[39mTry "how do I log an error?"\x1b[7m \x1b[0m'


def _build_styled_input_box_pane(rule_colour_sgr: str, styled_input_row: str, styled_footer: str) -> str:
    rule = rule_colour_sgr + "─" * 40
    return "\n".join((rule, styled_input_row, rule, styled_footer))


def build_styled_shell_mode_pane(styled_input_row: str) -> str:
    """The bottom of a shell-mode pane under `capture-pane -e`, with shortened rules and footer around the row."""
    return _build_styled_input_box_pane(
        rule_colour_sgr="\x1b[38;5;211m",
        styled_input_row=styled_input_row,
        styled_footer="\x1b[39m  \x1b[38;5;211m! for shell mode\x1b[39m",
    )


def build_styled_prompt_pane(styled_input_row: str) -> str:
    """The bottom of a normal-mode pane under `capture-pane -e`, with shortened rules and footer around the row."""
    return _build_styled_input_box_pane(
        rule_colour_sgr="\x1b[38;5;244m",
        styled_input_row=styled_input_row,
        styled_footer="\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m",
    )
