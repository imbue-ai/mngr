"""Test script for verifying kqueue + tty compatibility.

This script is NOT imported as a module -- it is read as a resource file
by test_urwid_tty.py and executed in a tmux session to verify that the
resolved tty path works with macOS kqueue in both piped-stdin and
direct-stdin contexts.

The sentinel ``URWID_TTY_TEST_DONE`` is printed at the end so the test
harness knows when execution has finished.
"""

import selectors
import socket

from imbue.mngr.cli.urwid_utils import resolve_real_tty_path

path = resolve_real_tty_path()
print(f"resolved_tty_path={path}")

tty_file = open(path)
try:
    rd, wr = socket.socketpair()
    rd.setblocking(False)
    sel = selectors.DefaultSelector()
    try:
        sel.register(rd, selectors.EVENT_READ)
        sel.register(tty_file, selectors.EVENT_READ)
        print("kqueue_register=OK")
    except OSError as e:
        print(f"kqueue_register=FAILED: {e}")
    finally:
        sel.close()
        rd.close()
        wr.close()
finally:
    tty_file.close()

print("URWID_TTY_TEST_DONE")
