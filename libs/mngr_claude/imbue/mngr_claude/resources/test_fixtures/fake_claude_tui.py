"""Pane program standing in for Claude Code's TUI in send tests.

This script is NOT imported as a module -- it is read as a resource file by
test_claude_agent_send.py and run in a tmux pane. It draws Claude's column-0
input prompt, echoes what is typed or pasted after it, and on Enter appends the
``user`` record Claude writes to its session transcript (the file named by its
first argument), so a send confirms only if the text and the Enter genuinely
reached the pane. Like Claude, it turns on bracketed paste, ignores Enter on an
empty input box, and takes a lone ``?`` keystroke on an empty input box as the
shortcuts-panel key rather than text. It draws straight to its terminal, which
is the whole point of it, so it is excluded from the bare-print ratchet. It
exits when its input closes.
"""

import json
import os
import sys
import tty

INPUT_PROMPT = "❯ "
READ_SIZE_BYTES = 4096
ENABLE_BRACKETED_PASTE = "\x1b[?2004h"
PASTE_START_MARKER = "\x1b[200~"
PASTE_END_MARKER = "\x1b[201~"
SHORTCUTS_PANEL_KEY = "?"

transcript_path = sys.argv[1]
file_descriptor = sys.stdin.fileno()


def draw_input_row(typed_text: str) -> None:
    sys.stdout.write("\r\x1b[K" + INPUT_PROMPT + typed_text)
    sys.stdout.flush()


def append_user_record(typed_text: str) -> None:
    os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
    with open(transcript_path, "a") as transcript_file:
        transcript_file.write(json.dumps({"type": "user", "message": {"role": "user", "content": typed_text}}) + "\n")


# Raw before drawing: setraw discards pending input, so a send that sees the prompt has nothing to lose.
tty.setraw(file_descriptor)
sys.stdout.write(ENABLE_BRACKETED_PASTE + "fake claude\r\n\r\n")
draw_input_row("")
typed = ""
while chunk := os.read(file_descriptor, READ_SIZE_BYTES):
    text = chunk.decode(errors="ignore")
    if text == SHORTCUTS_PANEL_KEY and typed == "":
        continue
    for char in text.replace(PASTE_START_MARKER, "").replace(PASTE_END_MARKER, ""):
        if char == "\r":
            if typed == "":
                continue
            append_user_record(typed)
            sys.stdout.write("\r\n")
            typed = ""
        else:
            typed += char
        draw_input_row(typed)
