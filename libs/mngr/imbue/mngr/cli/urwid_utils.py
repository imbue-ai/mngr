import os
import sys
import termios
from collections.abc import Generator
from contextlib import ExitStack
from contextlib import contextmanager

from urwid.display.raw import Screen


def has_interactive_terminal(
    *,
    stdin_is_tty: bool | None = None,
    tty_path: str = "/dev/tty",
) -> bool:
    """Return True if a real terminal is available for interactive TUI input.

    Checks sys.stdin first, then falls back to probing *tty_path*.  This
    handles the common case where stdin is piped (e.g. via ``uv run``)
    but a controlling terminal still exists.

    Parameters are exposed for testing; callers should use the defaults.
    """
    stdin_check = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if stdin_check:
        return True
    try:
        fd = os.open(tty_path, os.O_RDONLY)
        os.close(fd)
        return True
    except OSError:
        return False


@contextmanager
def create_urwid_screen_preserving_terminal() -> Generator[Screen, None, None]:
    """Create a urwid Screen that preserves terminal settings on exit.

    urwid's tty_signal_keys(intr="undefined") modifies termios to disable
    SIGINT at the terminal level. urwid does not reliably restore this on
    exit, which permanently breaks Ctrl+C for the rest of the terminal
    session. This context manager saves terminal settings before the Screen
    is created and restores them in a finally block.

    When sys.stdin is not a tty (e.g. piped through ``uv run``), the
    Screen reads input from /dev/tty instead so the TUI still works as
    long as a controlling terminal exists.
    """
    with ExitStack() as stack:
        if sys.stdin.isatty():
            tty_input = sys.stdin
        else:
            tty_input = stack.enter_context(open("/dev/tty"))

        saved_tty_attrs = termios.tcgetattr(tty_input)
        screen = Screen(input=tty_input)
        screen.tty_signal_keys(intr="undefined")
        try:
            yield screen
        finally:
            termios.tcsetattr(tty_input, termios.TCSADRAIN, saved_tty_attrs)
