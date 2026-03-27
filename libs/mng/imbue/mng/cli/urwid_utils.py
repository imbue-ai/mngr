import sys
import termios
from collections.abc import Generator
from contextlib import contextmanager

from urwid.display.raw import Screen


@contextmanager
def create_urwid_screen_preserving_terminal() -> Generator[Screen, None, None]:
    """Create a urwid Screen that preserves terminal settings on exit.

    urwid's tty_signal_keys(intr="undefined") modifies termios to disable
    SIGINT at the terminal level. urwid does not reliably restore this on
    exit, which permanently breaks Ctrl+C for the rest of the terminal
    session. This context manager saves terminal settings before the Screen
    is created and restores them in a finally block.
    """
    saved_tty_attrs = termios.tcgetattr(sys.stdin)
    screen = Screen()
    screen.tty_signal_keys(intr="undefined")
    try:
        yield screen
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved_tty_attrs)
