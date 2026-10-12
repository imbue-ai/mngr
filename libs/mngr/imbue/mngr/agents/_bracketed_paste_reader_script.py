"""Pane program that asks for bracketed paste and records what it is handed.

This script is NOT imported as a module -- it is read as a resource file by
base_agent_test.py and run in a tmux pane, to verify that a long message is
delivered as one bracketed paste rather than as loose keystrokes.

It writes the escape sequence that requests bracketed-paste mode straight to
its terminal, which is the whole point of it, so it is excluded from the
bare-print ratchet. Every byte the pane receives goes to the file named by its
first argument, until the paste's end marker arrives.
"""

import os
import sys
import termios
import tty

REQUEST_BRACKETED_PASTE = "\x1b[?2004h"
PASTE_END_MARKER = b"\x1b[201~"
READ_SIZE_BYTES = 65536

output_path = sys.argv[1]
file_descriptor = sys.stdin.fileno()
original_terminal_state = termios.tcgetattr(file_descriptor)
tty.setraw(file_descriptor)
sys.stdout.write(REQUEST_BRACKETED_PASTE)
sys.stdout.flush()
received = b""
with open(output_path, "wb") as received_file:
    try:
        while PASTE_END_MARKER not in received:
            chunk = os.read(file_descriptor, READ_SIZE_BYTES)
            if not chunk:
                break
            received_file.write(chunk)
            received_file.flush()
            received += chunk
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal_state)
