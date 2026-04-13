import os
import sys
import termios
from collections.abc import Generator
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path

from urwid.display.raw import Screen


def has_interactive_terminal(
    *,
    stdin_is_tty: bool | None = None,
    tty_path: Path = Path("/dev/tty"),
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


def resolve_real_tty_path() -> str:
    """Return the path to the real pty device for the controlling terminal.

    macOS kqueue does not support EVFILT_READ on ``/dev/tty`` (the virtual
    controlling-terminal device), returning EINVAL.  It *does* work on the
    actual pty device (e.g. ``/dev/ttys003``).  This function resolves the
    real device path by inspecting stdout/stderr, then by probing the
    controlling terminal via ``/dev/tty``, falling back to ``/dev/tty``
    as a last resort (which still works on Linux where epoll is used
    instead of kqueue, provided a controlling terminal exists).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
            if os.isatty(fd):
                return os.ttyname(fd)
        except (OSError, ValueError):
            continue

    # stdout/stderr aren't TTYs (e.g. piped by a test harness), but there
    # may still be a controlling terminal. Open /dev/tty to get a fd, then
    # resolve the real device path from it.
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            return os.ttyname(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

    return "/dev/tty"


@contextmanager
def create_urwid_screen_preserving_terminal() -> Generator[Screen, None, None]:
    """Create a urwid Screen that preserves terminal settings on exit.

    urwid's tty_signal_keys(intr="undefined") modifies termios to disable
    SIGINT at the terminal level. urwid does not reliably restore this on
    exit, which permanently breaks Ctrl+C for the rest of the terminal
    session. This context manager saves terminal settings before the Screen
    is created and restores them in a finally block.

    When sys.stdin is not a tty (e.g. piped through ``curl | bash``), the
    Screen reads input from the real pty device so the TUI still works as
    long as a controlling terminal exists.
    """
    tty_source = open(resolve_real_tty_path()) if not sys.stdin.isatty() else nullcontext(sys.stdin)
    with tty_source as tty_input:
        saved_tty_attrs = termios.tcgetattr(tty_input)
        screen = Screen(input=tty_input)
        screen.tty_signal_keys(intr="undefined")
        try:
            yield screen
        finally:
            termios.tcsetattr(tty_input, termios.TCSADRAIN, saved_tty_attrs)
