"""Pane program standing in for Claude Code's TUI in send tests.

This script is NOT imported as a module -- it is read as a resource file by
test_claude_agent_send.py and run in a tmux pane. It draws Claude's column-0
input prompt, echoes what is typed after it, and on Enter appends the ``user``
record Claude writes to its session transcript (the file named by its first
argument), so a send confirms only if the typed text and the Enter genuinely
reached the pane. It draws straight to its terminal, which is the whole point
of it, so it is excluded from the bare-print ratchet. It exits when its input
closes.
"""

import json
import os
import sys
import tty

INPUT_PROMPT = "❯ "
READ_SIZE_BYTES = 4096

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
sys.stdout.write("fake claude\r\n\r\n")
draw_input_row("")
typed = ""
while chunk := os.read(file_descriptor, READ_SIZE_BYTES):
    for char in chunk.decode(errors="ignore"):
        if char == "\r":
            append_user_record(typed)
            sys.stdout.write("\r\n")
            typed = ""
        else:
            typed += char
        draw_input_row(typed)
