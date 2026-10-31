"""Claude Code TUI dialogs, for 2.1.307.

A SNAPSHOT OF ONE BINARY. Every pattern and option label below was read out of the
shipped claude 2.1.307 executable. On a version bump, rewrite this file against the
new binary and change ``CLAUDE_CODE_VERSION`` -- do not annotate what moved, do not
keep patterns "for compatibility". ``test_patterns_match_installed_binary`` asserts every
pattern here still matches the claude build ``CLAUDE_CODE_VERSION`` names, and that check
is only meaningful if nothing in the file is historical.

Three kinds of surface, told apart by what answering one costs:

  BENIGN, RECOGNISED       Esc dismisses it and the conversation is untouched. Always
                           handled, never surfaced, never named in config -- there is no
                           decision to delegate.
  NON-BENIGN, RECOGNISED   A confirmation holding the input, where a wrong answer changes
                           something. Answered only if the operator opted in
                           (``sensibly_deal_with_dialogs``), and only ever on the option
                           this module names for it.
  UNRECOGNISED             Something owns the input and we cannot name it, so we cannot
                           know whether answering it is benign. Raised.

"Benign" is the axis that decides all of this. It is not about how a surface looks: the
model-switch confirmation and the theme picker are both selectors, and one of them changes
the session. It is about whether mngr dealing with the surface itself can be regretted.

The pane predicates below (shell mode, the input prompt) live here too, so one module
owns "how to read a claude screen".
"""

from __future__ import annotations

import re
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import Final
from typing import Protocol

from rich.ansi import AnsiDecoder
from rich.style import Style
from rich.text import Text

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The claude build these patterns were read out of. It must equal the version the mngr
# Dockerfile installs.
CLAUDE_CODE_VERSION: Final[str] = "2.1.307"


# Pane predicates

# Claude Code's input-prompt glyph. Context decides what it means: a line that
# BEGINS with it (column 0, no leading whitespace) is the input box; the same glyph
# indented (`  ❯ 1. ...`) marks the highlighted option of a multiple-choice selector.
INPUT_PROMPT_GLYPH: Final[str] = "❯"
# A line that begins with the input-prompt glyph at column 0 (the input box, not a selector).
INPUT_PROMPT_LINE_RE: Final[re.Pattern[str]] = re.compile(rf"^{INPUT_PROMPT_GLYPH}", re.MULTILINE)
# The highlighted option of a selector: indented, arrow, number, dot -- e.g. "  ❯ 1.".
# The required leading whitespace is what distinguishes it from the column-0 input prompt.
SELECTOR_HIGHLIGHTED_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]+❯[ \t]*\d+\.", re.MULTILINE)

# Claude Code's shell (bash) mode: typing `!` at an empty prompt swaps the column-0 `❯`
# input glyph for `!` and shows the shell-mode footer, and submitting an empty shell line
# is a no-op that STAYS in shell mode, hiding the `❯` prompt. A single Backspace deletes
# the `!` and returns to normal mode.
SHELL_MODE_FOOTER_TEXT: Final[str] = "! for shell mode"


# How much of the pane's bottom counts as the input area. Claude draws the input box -- or
# whatever surface has replaced it -- at the bottom, so every "is the input box here" question
# is asked of this tail rather than of the whole capture. It cannot be asked of the whole
# capture: Claude echoes every past user turn with the prompt glyph at column 0, so a
# whole-pane search matches the transcript in any conversation that has history.
# Sized for the real bottom chrome (rule / input row / rule / hint) plus a wrapping statusLine.
INPUT_REGION_LINES: Final[int] = 12


@pure
def get_input_region(pane_content: str) -> str:
    """The bottom of the pane: the last :data:`INPUT_REGION_LINES` non-blank lines.

    Blank lines are dropped first, so the region is unaffected by however much padding a
    surface leaves above its content.
    """
    non_blank = [line for line in pane_content.splitlines() if line.strip()]
    return "\n".join(non_blank[-INPUT_REGION_LINES:])


# How much of the pane's bottom a footer-only match may look at. Tighter than the input
# region: a footer is the LAST thing a dialog draws, and this is the only guard standing
# between a quoted "esc to cancel" and a real Escape keypress.
BENIGN_FOOTER_REGION_LINES: Final[int] = 3


@pure
def get_benign_footer_region(pane_content: str) -> str:
    """The last :data:`BENIGN_FOOTER_REGION_LINES` non-blank lines of the pane."""
    non_blank = [line for line in pane_content.splitlines() if line.strip()]
    return "\n".join(non_blank[-BENIGN_FOOTER_REGION_LINES:])


# What Claude puts at the start of the input row in shell mode, in place of the usual glyph.
SHELL_MODE_INPUT_GLYPH: Final[str] = "!"


@pure
def get_input_row(pane_content: str) -> str | None:
    """The pane's input row, or None when no surface on screen is holding one.

    The lowest line in the input region that starts an input row: the usual prompt glyph, or
    ``!`` when shell mode has replaced it. Taken from the BOTTOM because the rows above it are
    the transcript, where Claude renders each past user turn with the very same prompt glyph.
    """
    lines = pane_content.splitlines()
    input_row_idx = _find_input_row_idx(lines)
    return None if input_row_idx is None else lines[input_row_idx]


@pure
def _find_input_row_idx(lines: Sequence[str]) -> int | None:
    """The index of the input row :func:`get_input_row` describes, or None."""
    non_blank_idxs = [idx for idx, line in enumerate(lines) if line.strip()]
    for idx in reversed(non_blank_idxs[-INPUT_REGION_LINES:]):
        if lines[idx].startswith(INPUT_PROMPT_GLYPH) or lines[idx].startswith(SHELL_MODE_INPUT_GLYPH):
            return idx
    return None


@pure
def get_pane_text_without_input_row_hints(styled_pane_content: str) -> str:
    """The text of a ``capture-pane -e`` capture, as ``capture-pane -p`` gives it, without the hints in the input row.

    A hint is text Claude draws in the input row that nobody typed: the placeholder in an empty
    box (``Try "how do I log an error?"``, ``Press up to edit queued messages``) and completion
    ghost text after typed text. Claude draws every hint dim and never draws typed text dim. A
    capture without styling has nothing dim, so its hints read as typed text.
    """
    decoder = AnsiDecoder()
    styled_lines = [decoder.decode_line(line) for line in styled_pane_content.split("\n")]
    plain_lines = [line.plain.rstrip(" ") for line in styled_lines]
    input_row_idx = _find_input_row_idx(plain_lines)
    return "\n".join(
        _get_input_row_text_without_hints(styled_lines[idx]) if idx == input_row_idx else line
        for idx, line in enumerate(plain_lines)
    ).rstrip()


@pure
def _get_input_row_text_without_hints(styled_input_row: Text) -> str:
    """The input row without its hints, keeping the glyph that starts it however Claude styles it."""
    return (styled_input_row.plain[:1] + _get_text_without_hints(styled_input_row[1:])).rstrip(" ")


@pure
def _get_text_without_hints(styled_text: Text) -> str:
    """The text without its dim characters or the reverse-video cursor Claude draws over a hint's first character."""
    style_by_char_idx = _get_style_by_char_idx(styled_text)
    return "".join(
        char
        for idx, char in enumerate(styled_text.plain)
        if not style_by_char_idx[idx].dim
        and not (
            style_by_char_idx[idx].reverse and _is_next_visible_char_dim(styled_text.plain, style_by_char_idx, idx)
        )
    )


@pure
def _get_style_by_char_idx(styled_text: Text) -> list[Style]:
    style_by_char_idx = [Style.null()] * len(styled_text.plain)
    for span in styled_text.spans:
        span_style = span.style if isinstance(span.style, Style) else Style.parse(span.style)
        style_by_char_idx[span.start : span.end] = [span_style] * (span.end - span.start)
    return style_by_char_idx


@pure
def _is_next_visible_char_dim(text: str, style_by_char_idx: Sequence[Style], char_idx: int) -> bool:
    for idx in range(char_idx + 1, len(text)):
        if not text[idx].isspace():
            return bool(style_by_char_idx[idx].dim)
    return False


@pure
def has_input_prompt_line(pane_content: str) -> bool:
    """Whether Claude Code's input box is on screen -- a line beginning with the glyph at column 0.

    Asked of :func:`get_input_region`, not the whole pane. The glyph alone does not identify
    the input box: Claude renders each past user turn the same way, so a whole-pane search
    reports "the box is here" for any conversation with history, however the pane is actually
    occupied. Position is what distinguishes the live box from its echoes.
    """
    return INPUT_PROMPT_LINE_RE.search(get_input_region(pane_content)) is not None


@pure
def _alnum_lower(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalnum())


@pure
def is_input_text_of_message(input_text: str, message: str, paste_placeholder_prefixes: Sequence[str]) -> bool:
    """Whether text in the input box is the start of ``message``: words that begin it, or a paste's placeholder."""
    if any(input_text.startswith(prefix) for prefix in paste_placeholder_prefixes):
        return True
    words = _alnum_lower(input_text)
    return words != "" and _alnum_lower(message).startswith(words)


@pure
def count_message_turns_above_input_row(
    pane_content: str, message: str, paste_placeholder_prefixes: Sequence[str]
) -> int | None:
    """How many past user turns above the input row read as ``message``, or None when no input row is on screen.

    Claude renders each submitted message at column 0 behind the prompt glyph, showing its first
    line (or the placeholder of a long paste). A turn reads as the message when it is a paste
    placeholder or its words begin the message's, so an earlier turn can read as it too: only a
    count that grew past one taken before the send says this send's turn is on screen.
    """
    lines = pane_content.splitlines()
    input_row_idx = _find_input_row_idx(lines)
    if input_row_idx is None:
        return None
    return sum(
        1
        for line in lines[:input_row_idx]
        if line.startswith(INPUT_PROMPT_GLYPH)
        and is_input_text_of_message(line[len(INPUT_PROMPT_GLYPH) :].strip(), message, paste_placeholder_prefixes)
    )


@pure
def is_shell_command_message(message: str) -> bool:
    """Whether a message drives Claude Code's shell (bash) mode -- a leading ``!``.

    Such a message runs a bash command in the pane (or, for a bare ``!``, does nothing)
    rather than sending a model turn.
    """
    return message.lstrip().startswith("!")


# The command Claude Code puts back into the input box when a compaction is cancelled.
COMPACT_COMMAND: Final[str] = "/compact"


@pure
def is_restored_compact_command(input_text: str) -> bool:
    """Whether input-box text is a ``/compact`` command, bare or with arguments.

    Cancelling a ``/compact`` restores ``/compact`` (followed by the dim argument hint when it
    had no instructions) or ``/compact <instructions>`` to the input box.
    """
    return input_text == COMPACT_COMMAND or input_text.startswith(COMPACT_COMMAND + " ")


@pure
def _get_shell_mode_input_text(pane_content: str) -> str | None:
    """The input row's text after the shell-mode ``!``, or None when shell mode is not holding the input.

    Whitespace alone means an empty command line. Expects a pane read through
    :func:`get_pane_text_without_input_row_hints`; on a raw capture the placeholder in an empty
    box reads as a typed command.

    Both the row and the footer are read from the input region, never from the whole pane: the
    footer text and a bare ``!`` row both occur in ordinary transcript content -- a conversation
    that merely discusses shell mode contains them.
    """
    if SHELL_MODE_FOOTER_TEXT not in get_input_region(pane_content):
        return None
    input_row = get_input_row(pane_content)
    if input_row is None or not input_row.startswith(SHELL_MODE_INPUT_GLYPH):
        return None
    return input_row[len(SHELL_MODE_INPUT_GLYPH) :]


@pure
def is_stranded_in_empty_shell_mode(pane_content: str) -> bool:
    """Whether the pane is stranded in Claude's shell mode on an empty command line -- the state a bare ``!`` submission leaves behind."""
    input_text = _get_shell_mode_input_text(pane_content)
    return input_text is not None and input_text.strip() == ""


@pure
def is_pending_shell_command(pane_content: str) -> bool:
    """Whether the pane is in Claude's shell mode holding an unsubmitted (non-empty) command.

    Within shell mode this is the complement of :func:`is_stranded_in_empty_shell_mode`: the
    footer is up and the ``❯`` prompt is hidden, but the input row is not the empty strand. mngr
    never leaves this state -- its own sends always submit with Enter -- so it can only be a
    command a human typed directly into the pane and did not submit. Reads the input row, never a
    ``!`` line elsewhere, so a prior ``!<command>`` echoed in the transcript is not mistaken for
    the pending input.
    """
    input_text = _get_shell_mode_input_text(pane_content)
    return input_text is not None and input_text.strip() != ""


@pure
def is_option_highlighted(pane_content: str, option_label: str) -> bool:
    """Whether the selector's currently-highlighted option begins with ``option_label``.

    Used to land on a specific option before pressing Enter, so answering a dialog never
    depends on which row claude happens to highlight.

    The label must start the option's text and be followed by whitespace or the end of the
    option, because one option can be the start of another: the plugin prompt offers both
    ``No`` and ``No, and don't show plugin installation hints again``, and a containment check
    would land on either.
    """
    highlighted_option_text: str | None = None
    for line in pane_content.splitlines():
        option_prefix = SELECTOR_HIGHLIGHTED_OPTION_RE.match(line)
        if option_prefix is not None:
            # The LAST one: an earlier
            # arrow row is a stale selector scrolled above the live dialog.
            highlighted_option_text = line[option_prefix.end() :].strip()
    if highlighted_option_text is None or not highlighted_option_text.startswith(option_label):
        return False
    rest = highlighted_option_text[len(option_label) :]
    return rest == "" or rest[0].isspace()


# What a dialog needs from the pane


class DialogPane(Protocol):
    """The pane operations a dialog performs.

    A protocol rather than the agent itself, so this module stays free of mngr's agent
    machinery and every class here is testable against a fake. ``ClaudeAgent`` supplies a
    small adapter that binds its ``tmux_target`` and agent config.
    """

    def capture(self) -> str:
        """The pane's current visible content, read through :func:`get_pane_text_without_input_row_hints`."""
        ...

    def press_enter(self) -> None: ...

    def press_down(self) -> None: ...

    def press_key(self, key: str) -> None:
        """Send one tmux key token (``Escape``, ``BSpace``)."""
        ...

    def accepts(self, nickname: str) -> bool:
        """Whether ``sensibly_deal_with_dialogs`` opted into answering this dialog."""
        ...


# Max Down presses before giving up on reaching a named option. Selectors are short and
# the highlight wraps, so every option is reachable well inside this; it is a runaway guard.
_MAX_OPTION_STEPS: Final[int] = 12

# Passes over a chain of dialogs before giving up. Dismissing one can reveal another; the
# no-progress check catches a stuck dialog long before this, so this only breaks a cycle.
_MAX_DIALOG_PASSES: Final[int] = 4


def cycle_to_option(pane: DialogPane, option_label: str, max_steps: int = _MAX_OPTION_STEPS) -> bool:
    """Walk the selector's highlight onto the option that begins with ``option_label``.

    "Begins with" as :func:`is_option_highlighted` defines it: whitespace or the option's end follows the label.

    Re-captures after every Down press rather than counting offsets, so it is correct
    regardless of where the highlight started or how the options are ordered. Returns
    False when the option is not reachable, which callers treat as a refusal: pressing
    Enter on the wrong row is precisely the failure this exists to avoid.

    ``pane.press_down`` must not return until the pane has actually changed. tmux
    ``send-keys`` is asynchronous, so a capture issued straight afterwards still shows the
    old highlight; without that wait this loop would fire every Down before the TUI
    redrew and then refuse a reachable option.
    """
    pane_content = pane.capture()
    if option_label not in pane_content:
        # Not on screen at all. Refuse before pressing anything: cycling would leave the
        # user's selector parked on an arbitrary row before we tell them to answer it.
        return False
    if SELECTOR_HIGHLIGHTED_OPTION_RE.search(pane_content) is None:
        # The words are on screen but there is no selector to move. That happens when the text
        # is transcript rather than a dialog -- UsageLimitReached's caption IS its option label,
        # so an agent explaining the usage limit contains both -- and without this the walk below
        # would press Down a dozen times into whatever has the input, which for a live composer
        # means a dozen keystrokes the user never typed.
        return False
    for _ in range(max_steps):
        if is_option_highlighted(pane_content, option_label):
            return True
        pane.press_down()
        pane_content = pane.capture()
    return False


# Dialog kinds


class DialogBlocked(Exception):
    """A dialog stopped a send. Carries the words the client shows the user.

    ``mngr_claude.plugin`` maps this onto its own ``SendMessageError`` subclasses, which
    differ by whether the message had already been delivered -- a distinction this module
    has no opinion about.
    """

    def __init__(self, nickname: str, message: str) -> None:
        self.nickname = nickname
        self.message = message
        super().__init__(f"{nickname}: {message}")


class Dialog(FrozenModel, ABC):
    """Something occupying the pane that a send must deal with.

    Two independent axes, and only the second is worth a class hierarchy:

      how it is recognised -- a caption (most), or a structural predicate (shell mode,
                              Unrecognized). ``MatchesPattern`` covers the common case.
      what to do about it  -- clear it, answer it, or refuse. That is ``deal_with``, and
                              it is why these are classes rather than a table of tuples.
    """

    @abstractmethod
    def get_nickname(self) -> str:
        """Stable name. For answerable dialogs this is also the config key, so it is a
        published contract -- never change one once shipped."""

    @abstractmethod
    def get_message(self) -> str:
        """What the user is told when this stops a send.

        Describes the situation and the action. Never names a config field (the modal is
        for the user, not the operator) and never quotes the pane (unbounded, and it can
        contain the user's own code or a diff).
        """

    @abstractmethod
    def matches(self, pane_content: str) -> bool: ...

    @abstractmethod
    def deal_with(self, pane: DialogPane) -> None:
        """Clear this, or raise :class:`DialogBlocked`."""


class MatchesPattern(Dialog, ABC):
    """Recognised by searching the pane for a pattern -- the common case."""

    @abstractmethod
    def get_pattern(self) -> re.Pattern[str]: ...

    def matches(self, pane_content: str) -> bool:
        return self.get_pattern().search(pane_content) is not None


class SelfClearing(Dialog, ABC):
    """mngr can clear this itself and the conversation is untouched.

    Deliberately has NO answer path. Several clearable surfaces are destructive on Enter
    -- the rewind picker rewinds the conversation, the usage-limit upsell buys credits --
    so this can only send its dismiss key, and no later edit can hand one an Enter path
    by accident.

    Escape clears almost everything; shell mode overrides the key.
    """

    def get_dismiss_key(self) -> str:
        return "Escape"

    def get_message(self) -> str:
        # Never raises, so there is nothing to say.
        return ""

    def deal_with(self, pane: DialogPane) -> None:
        pane.press_key(self.get_dismiss_key())


class Answerable(Dialog, ABC):
    """A confirmation holding the input. Answered, if the operator opted in by nickname.

    NEVER presses Enter on whatever happens to be highlighted. Every subclass names the
    option it wants and the selector is cycled onto it first. The highlight is not ours:
    the pane is shared with anyone attached to the tmux session, so one stray arrow key --
    or a claude release that reorders the options -- would otherwise turn "confirm the
    switch" into "buy credits".

    If the named option is never reached the send REFUSES. A wrong or stale label degrades
    to a clean error, never to a wrong answer.
    """

    @abstractmethod
    def get_option_label(self) -> str:
        """The start of the option to land on before pressing Enter, up to whitespace or the option's end."""

    def deal_with(self, pane: DialogPane) -> None:
        if not pane.accepts(self.get_nickname()):
            raise DialogBlocked(self.get_nickname(), self.get_message())
        if not cycle_to_option(pane, self.get_option_label()):
            raise DialogBlocked(self.get_nickname(), self.get_message())
        pane.press_enter()


class Blocking(Dialog, ABC):
    """Needs a human. mngr will not answer it."""

    def deal_with(self, pane: DialogPane) -> None:
        raise DialogBlocked(self.get_nickname(), self.get_message())


# ACCEPT -- selectable in `sensibly_deal_with_dialogs`


class ModelSwitchWarning(MatchesPattern, Answerable):
    """Raised after choosing an entry in the ``/model`` picker: switching mid-session
    invalidates the prompt cache, so claude asks to confirm.

    The case this whole feature exists for. It opens *because of* the message just sent,
    so only the post-submit check can see it.
    """

    def get_nickname(self) -> str:
        return "Model switch warning"

    def get_pattern(self) -> re.Pattern[str]:
        # The sentence is assembled at runtime and spans ~110 chars, so capture-pane's
        # one-line-per-row output wraps it. `\s+` spans that newline -- DOTALL would not,
        # since the pattern contains no `.`.
        return re.compile(r"This\s+conversation\s+is\s+cached\s+for\s+the\s+current\s+model")

    def get_option_label(self) -> str:
        # Renders as "Yes, switch to <model>".
        return "Yes, switch to"

    def get_message(self) -> str:
        return "Claude is waiting for you to confirm a model switch. Answer it in the agent's terminal."


class EffortSwitchWarning(MatchesPattern, Answerable):
    """The effort-level variant of the same component as :class:`ModelSwitchWarning`."""

    def get_nickname(self) -> str:
        return "Effort switch warning"

    def get_pattern(self) -> re.Pattern[str]:
        return re.compile(r"This\s+conversation\s+is\s+cached\s+for\s+the\s+current\s+effort\s+level")

    def get_option_label(self) -> str:
        return "Yes, switch to"

    def get_message(self) -> str:
        return "Claude is waiting for you to confirm an effort-level change. Answer it in the agent's terminal."


class UsageLimitReached(MatchesPattern, Answerable):
    """Out of usage: stop and wait, continue on credits, or buy more."""

    def get_nickname(self) -> str:
        return "Usage limit reached"

    def get_pattern(self) -> re.Pattern[str]:
        return re.compile(r"Stop and wait for limit to reset")

    def get_option_label(self) -> str:
        # The other options buy credits or upgrade the plan -- never mngr's call.
        return "Stop and wait for limit to reset"

    def get_message(self) -> str:
        return "Claude has hit its usage limit and is waiting for you to choose how to proceed. Answer it in the agent's terminal."


class LspPluginInstall(MatchesPattern, Answerable):
    """Offers to install an LSP plugin for the current project."""

    def get_nickname(self) -> str:
        return "LSP plugin install"

    def get_pattern(self) -> re.Pattern[str]:
        return re.compile(r"Would you like to install this LSP plugin\?")

    def get_option_label(self) -> str:
        # Declining is the inert answer; installing a plugin is not mngr's call, and the other
        # refusals ("Never for this plugin", "Disable all LSP recommendations") change settings.
        return "No, not now"

    def get_message(self) -> str:
        return "Claude is waiting for you to say whether to install an LSP plugin. Answer it in the agent's terminal."


class PluginInstall(MatchesPattern, Answerable):
    """Offers to install a plugin that the command just run suggests."""

    def get_nickname(self) -> str:
        return "Plugin install"

    def get_pattern(self) -> re.Pattern[str]:
        # Its third option, not its question: "Would you like to install it?" is a sentence an
        # agent's own reply can end with, and the transcript is searched before the input box.
        return re.compile(r"No,\s+and\s+don't\s+show\s+plugin\s+installation\s+hints\s+again")

    def get_option_label(self) -> str:
        # Declining is the inert answer. The first option installs the plugin, and the third
        # ("No, and don't show plugin installation hints again") changes a setting.
        return "No"

    def get_message(self) -> str:
        return "Claude is waiting for you to say whether to install a plugin. Answer it in the agent's terminal."


# BENIGN -- always handled, never selectable


class StatusWindow(MatchesPattern, SelfClearing):
    """The ``/status`` pane.

    Its body changes with the selected tab, so match the TAB STRIP rather than any one
    tab's contents -- otherwise the class only fires on whichever tab happened to be open
    when the pattern was written.
    """

    def get_nickname(self) -> str:
        return "Status window"

    def get_pattern(self) -> re.Pattern[str]:
        # The tabs are rendered side by side on ONE row, so the separator is spaces or tabs
        # -- never `\s`, which also matches a newline and would let five bare words on
        # consecutive transcript lines (a list, a table, a --help dump) match. This class is
        # self-clearing, so that false positive costs a real Escape on a live turn.
        return re.compile(r"Settings[ \t]+Status[ \t]+Config[ \t]+Usage[ \t]+Stats")


class GenericBenign(MatchesPattern, SelfClearing):
    """Any dialog whose footer offers an Esc dismissal.

    This one class is why the named list stays short. Claude renders a footer on every
    dismissible surface -- ``Enter to select · Esc to cancel``, ``… · Esc to close`` -- and
    that footer is a complete signal on its own: if Esc closes it, the conversation is
    untouched and there is nothing to tell anyone about.

    Covers at least the following, by the slash command that opens each:

        /theme       the text-style picker (also shown during onboarding)
        /model       the model picker, and the effort picker behind it
        /bug         the bug-report form
        /chrome      the Chrome-extension prompt
        /mcp         the MCP server list
        /skills      the skills list
        /btw         the "by the way" note prompt
        /config      the settings screen (/settings is the same surface)
        /diff        the diff viewer
        /feedback    the feedback prompt
        /hooks       the hooks editor
        /status      handled by StatusWindow above, which matches its tab strip
        (login)      the OAuth device-code screen, not reached by a slash command

    ...and many more. The list is illustrative, NOT exhaustive, and deliberately so: the
    footer is the whole signal, so this class also covers every dismissible surface nobody
    has enumerated -- including ones claude has not shipped yet. An app we do not control
    adds dialogs on its own schedule, which is work a hand-maintained list cannot do.

    NOT covered, deliberately -- these look like footers but Esc does something real:

        "Esc to stop"    kills the running turn ("Claude is using your computer")
        "esc to rewind"  rewinds the conversation
        "esc to edit"    enters edit mode
        "esc to keep"    one arm of a choice, not a dismissal
        "esc to clear"   clears the COMPOSER INPUT -- not a dialog at all; that is
                         the send path's pre-existing-input concern

    Tried AFTER every named class. Several ACCEPT dialogs carry an ``Esc to cancel`` footer
    of their own, so matching this first would Escape a model-switch confirmation and
    silently cancel the user's own change.
    """

    def get_nickname(self) -> str:
        return "Benign dialog"

    def get_pattern(self) -> re.Pattern[str]:
        # Case-insensitive: claude ships both capitalisations -- "Esc to cancel" in the
        # pickers, lowercase "esc to cancel"/"esc to close" in the resume-session picker
        # and the menu screen.
        return re.compile(r"esc to (?:close|cancel)", re.IGNORECASE)

    def matches(self, pane_content: str) -> bool:
        # Only near the bottom. This class recognises a dialog by its footer alone, so a
        # message whose TEXT quotes those words would otherwise look like one -- and this is
        # self-clearing, so the cost is a real Escape on a live turn.
        #
        # Three lines, and the number is structural rather than a guess: against the real
        # bottom chrome when the input box is drawn (rule / input row / hint) the last three
        # non-blank lines are exactly that chrome, and transcript content starts at the
        # fourth. Widening it would reach into the transcript; narrowing it would miss a
        # dialog whose footer has a hint or a wrapped row beneath it.
        return self.get_pattern().search(get_benign_footer_region(pane_content)) is not None


# SHELL MODE -- not a dialog, but the same shape


class EmptyShellMode(SelfClearing):
    """Claude is stranded in shell mode on an empty command line.

    Submitting a lone ``!`` runs nothing and STAYS in shell mode, which hides the ``❯``
    prompt -- so the next send would wait for a prompt that never returns. Only mngr's own
    send can produce this (its sends always submit with Enter), so mngr self-heals it.

    Self-clearing exactly like a dismissible dialog; only the key differs.
    """

    def get_nickname(self) -> str:
        return "Empty shell mode"

    def matches(self, pane_content: str) -> bool:
        return is_stranded_in_empty_shell_mode(pane_content)

    def get_dismiss_key(self) -> str:
        # One Backspace deletes the `!` and returns to normal mode.
        return "BSpace"


class PendingShellCommand(Blocking):
    """A human typed ``!<command>`` into the pane and did not submit it.

    Shell mode hides the ``❯`` prompt, so a send would otherwise die in a readiness
    timeout with nothing to show for it. mngr will not submit or delete someone else's
    half-typed command, so it refuses with an actionable error instead -- the behaviour
    PR #397 established.
    """

    def get_nickname(self) -> str:
        return "Pending shell command"

    def get_message(self) -> str:
        return (
            "The agent is in shell mode with an unsubmitted command. Press Enter in its terminal "
            "to run it, or Escape to cancel it."
        )

    def matches(self, pane_content: str) -> bool:
        return is_pending_shell_command(pane_content)


# RAISE -- the floor


class Unrecognized(Dialog):
    """Something owns the input and nothing above matched.

    Detected by the ABSENCE of the column-0 prompt -- the same signal the shell predicates
    use -- not by finding a selector. Verified necessary against a live agent: claude's
    theme dialog has no ``─``/``▔`` rule line above its options (its separator is ``╌``),
    so a selector-shaped detector misses it entirely while the missing-prompt rule catches
    it.

    Refuses by default. Under ALL_KNOWN_AND_UNKNOWN_DIALOGS it presses "1" instead, which is
    a guess and is meant to read as one. Two things bound it, because the surface is unknown
    and the cost of a wrong press is not:

    * The digit is only sent to a pane showing a numbered selector. Without that check a "1"
      would be typed as text into whatever holds the input -- and the caller loops, so it would
      be typed repeatedly. This is the same guard that stopped the usage-limit caption from
      being walked as if it were its own option list.
    * Pressing is not succeeding. ``deal_with_dialogs`` re-classifies afterwards, refuses the
      send if the pane did not change, and gives up after its pass budget. So "1" that answers
      nothing still ends in the refusal this class started as.
    """

    def get_nickname(self) -> str:
        return _UNRECOGNIZED_NICKNAME

    def get_message(self) -> str:
        return (
            "Claude is waiting on something in its terminal that mngr does not recognise. Open the terminal to see it."
        )

    def matches(self, pane_content: str) -> bool:
        # Reached by classify() when nothing else matched and the input prompt is gone.
        return False

    def deal_with(self, pane: DialogPane) -> None:
        if not pane.accepts(self.get_nickname()):
            raise DialogBlocked(self.get_nickname(), self.get_message())
        if SELECTOR_HIGHLIGHTED_OPTION_RE.search(pane.capture()) is None:
            # Nothing a digit can answer. Refuse rather than type into it.
            raise DialogBlocked(self.get_nickname(), self.get_message())
        pane.press_key(UNKNOWN_DIALOG_ANSWER_KEY)


# The registry -- ORDER IS LOAD-BEARING

# classify() returns on first match, so:
#   1. named ACCEPT classes first -- several carry an "Esc to cancel" footer of their own,
#      and GenericBenign would Escape them
#   2. named BENIGN next
#   3. GenericBenign, the footer catch-all
# Unrecognized is not listed: it is reached structurally, by the missing input prompt.
DIALOGS: Final[tuple[Dialog, ...]] = (
    # Shell mode first. It is not a dialog, but it holds the input the same way and is dealt
    # with the same way: one self-clears with Backspace, the other needs a human. It leads
    # because PR #397's flow checked it ahead of any dialog handling, and because its evidence
    # is the strongest here -- the shell footer text appears on no ordinary pane (verified
    # against a live agent, both idle and with /config open), whereas the captions below are
    # matched wherever they occur.
    EmptyShellMode(),
    PendingShellCommand(),
    # Answerable next: several carry an "Esc to cancel" footer of their own, and GenericBenign
    # would otherwise Escape them -- silently cancelling the user's change.
    ModelSwitchWarning(),
    EffortSwitchWarning(),
    UsageLimitReached(),
    LspPluginInstall(),
    PluginInstall(),
    # Named self-clearing surfaces, then the footer catch-all.
    StatusWindow(),
    GenericBenign(),
)

# The two tokens `sensibly_deal_with_dialogs` accepts in place of a nickname.
#
# ALL_KNOWN_DIALOGS means "every dialog this module names, answered on the option it names".
# Each of those options was chosen by a person who looked at that dialog, so the set grows as
# the catalogue does without ever becoming a guess.
#
# ALL_KNOWN_AND_UNKNOWN_DIALOGS is that plus a fallback for surfaces nobody has named: press
# "1". It is a GUESS and is the one setting here that can be regretted -- an unnamed dialog's
# first option is unknown by definition, and for several named ones it would have been wrong
# (the usage-limit prompt's first option buys credits, both plugin prompts' install).
# It is bounded rather than trusted: the digit is only sent to a pane that actually shows a
# numbered selector, and if the surface does not go away the send still refuses.
#
# There is deliberately no wildcard: `*` invites being written to mean "all the ones you know",
# and a name that says which dialogs it covers cannot be misread that way.
# The nickname Unrecognized answers to. A module-level constant because the gate below has to
# name it before the class exists.
_UNRECOGNIZED_NICKNAME: Final[str] = "Unrecognized"

ALL_KNOWN_DIALOGS: Final[str] = "ALL_KNOWN_DIALOGS"
ALL_KNOWN_AND_UNKNOWN_DIALOGS: Final[str] = "ALL_KNOWN_AND_UNKNOWN_DIALOGS"

# What an unnamed surface is answered with. A digit, not a cycle-then-Enter: locating "option 1"
# by reading the highlight would need a pattern pinned to `1.`, and a highlighted `11.` row
# contains `1.` -- so the walk could land on the wrong row and press Enter on it. One keystroke
# has no such failure mode.
UNKNOWN_DIALOG_ANSWER_KEY: Final[str] = "1"

# The complete set of nicknames an operator may put in `sensibly_deal_with_dialogs`.
# Only non-benign recognised dialogs are selectable: benign ones are always dealt with
# (listing one would mean nothing) and unrecognised ones are not a decision anyone can
# delegate by name.
SELECTABLE_NICKNAMES: Final[frozenset[str]] = frozenset(
    dialog.get_nickname() for dialog in DIALOGS if isinstance(dialog, Answerable)
)


@pure
def is_nonbenign_answer_allowed(nickname: str, configured: tuple[str, ...]) -> bool:
    """Whether the operator opted into mngr answering the dialog called ``nickname``.

    An unnamed surface is covered ONLY by ALL_KNOWN_AND_UNKNOWN_DIALOGS, never by a token or a
    nickname that reads as being about dialogs mngr knows. Answering one is a guess, and opting
    into guessing has to be its own deliberate act rather than something a broader-sounding
    setting quietly includes.
    """
    if nickname == _UNRECOGNIZED_NICKNAME:
        return ALL_KNOWN_AND_UNKNOWN_DIALOGS in configured
    if ALL_KNOWN_DIALOGS in configured or ALL_KNOWN_AND_UNKNOWN_DIALOGS in configured:
        return True
    return nickname in configured


@pure
def classify(pane_content: str) -> Dialog | None:
    """Whatever is holding the pane's input, or None when nothing is.

    Covers shell mode as well as dialogs: both take the input box away and both are dealt
    with the same way, so the caller has one loop and no special cases.

    The catalogue is walked BEFORE the input box is looked for, and that order is
    load-bearing rather than stylistic. A dialog need not be tall: the `/model` switch
    confirmation is seven lines, so the echo of the user's own `/model` turn sits inside the
    input region directly above it. Asking about the box first reads that echo as a live
    prompt and returns None, missing the very confirmation this exists to catch. Each entry
    is recognised by its own evidence; the box is consulted only to tell "ready" from
    "something is here that we cannot name".
    """
    for dialog in DIALOGS:
        if dialog.matches(pane_content):
            return dialog
    if has_input_prompt_line(pane_content):
        # Nothing recognised owns the pane and the input box is on screen: ready to type.
        return None
    # The box is gone and nothing in the catalogue explains why.
    return Unrecognized()


def deal_with_dialogs(pane: DialogPane, max_passes: int = _MAX_DIALOG_PASSES) -> str:
    """Clear whatever is holding the pane's input and return the capture showing it free, or raise :class:`DialogBlocked`.

    These chain -- dismissing one can reveal another underneath -- so this loops rather than
    classifying once. The no-progress check is the real guard: the same thing still present
    after ``deal_with`` is stuck, not chained, so raise immediately instead of pressing the
    same key at it repeatedly. ``max_passes`` only breaks a genuine A -> B -> A cycle.

    Every keypress the pane performs waits for the pane to change before returning (tmux
    ``send-keys`` is asynchronous), so a pass never reads a screen that has not caught up.
    """
    previous_pane: str | None = None
    for _ in range(max_passes):
        pane_content = pane.capture()
        blocking = classify(pane_content)
        if blocking is None:
            return pane_content
        # Compare the PANE, not the nickname. GenericBenign covers every dismissible surface, so
        # it answers to one nickname for all of them -- and a genuine chain (the model picker
        # revealing the effort picker beneath it) would look identical to being stuck. An
        # unchanged pane after dealing with it is the honest signal that nothing moved.
        if pane_content == previous_pane:
            raise DialogBlocked(blocking.get_nickname(), blocking.get_message())
        blocking.deal_with(pane)
        previous_pane = pane_content
    # Report the dialog actually left on screen, not Unrecognized: something WAS recognised on
    # every pass, and the caller reads that nickname to decide whether the pane simply had not
    # painted yet -- a question a cycle between two known dialogs has already answered.
    raise DialogBlocked(blocking.get_nickname(), blocking.get_message())
