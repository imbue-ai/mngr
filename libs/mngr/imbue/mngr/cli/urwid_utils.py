import contextlib
import os
import sys
import termios
from collections.abc import Generator
from contextlib import contextmanager

from urwid.display.raw import Screen


def has_interactive_terminal() -> bool:
    """Return True if a real terminal is available for interactive TUI input.

    Checks sys.stdin first, then falls back to probing /dev/tty.  This
    handles the common case where stdin is piped (e.g. via ``uv run``)
    but a controlling terminal still exists.
    """
    if sys.stdin.isatty():
        return True
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
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
    with contextlib.ExitStack() as stack:
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
