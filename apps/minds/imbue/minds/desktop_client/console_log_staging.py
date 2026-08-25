"""Read the Electron shell's captured console output for a bug report.

The Electron main process records every renderer console message -- the minds SPA's own output and the
workspace iframe's -- into a rolling log file in the minds log dir (``electron/console-capture.js``).
That file rotates at 10MB keeping 10 gzipped rotations, the same bound ``electron.log`` and the Python
backend logs carry, and its name is one no Sentry attachment group globs.

Only the newest slice of it is read. The file is app-lifetime history bounded by that 10MB, but a
report wants the console *around the bug*, not everything the app has printed since it started. The
rotations are deliberately not read at all.

The reader hands the text to ``collect_workspace_diagnostics``, which stages it app-side as the
report's console attachment. The console never travels to the workspace and is deliberately not
secret-scanned -- the same standing as ``electron.log`` and ``minds.log``, which already upload
unscanned on every event. Reading the file (rather than a request to the shell) is the only channel
available: the desktop client is a subprocess the shell spawns, and every channel between the two
runs the other way.
"""

from pathlib import Path
from typing import Final

from loguru import logger

# Written by electron/console-capture.js; kept in step with ``CONSOLE_TAIL_FILENAME`` there.
ELECTRON_CONSOLE_TAIL_FILENAME: Final[str] = "console-tail.log"

# How much of the file's end to read, and how many of those records to keep. The byte ceiling is what
# actually bounds the work; the line cap then holds the excerpt to roughly the message count the
# capture used to bound the whole file at, so a report carries the same amount of console as before
# the file started rotating.
MAX_CONSOLE_TAIL_BYTES: Final[int] = 256 * 1024
MAX_CONSOLE_TAIL_LINES: Final[int] = 2000


def read_console_tail(logs_dir: Path) -> str | None:
    """The newest slice of the captured console, or None when the shell has captured nothing.

    None covers both a file that does not exist and one with nothing in it: the capture opens its
    stream (creating the file) as soon as the shell starts, so an empty file is the ordinary state
    before anything has been logged, and it must report as no console rather than as an empty
    attachment.

    Failures are logged rather than raised -- an unreadable console costs the report a file, not the
    report.
    """
    tail_path = logs_dir / ELECTRON_CONSOLE_TAIL_FILENAME
    try:
        size = tail_path.stat().st_size
        with tail_path.open("rb") as tail_file:
            is_truncated = size > MAX_CONSOLE_TAIL_BYTES
            if is_truncated:
                tail_file.seek(size - MAX_CONSOLE_TAIL_BYTES)
            raw = tail_file.read(MAX_CONSOLE_TAIL_BYTES)
    except FileNotFoundError:
        logger.info("No captured console output at {} to attach to this report", tail_path)
        return None
    except OSError as exc:
        logger.warning("Could not read the captured console output from {}: {}", tail_path, exc)
        return None

    lines = raw.decode("utf-8", errors="replace").splitlines()
    # Seeking into the middle of the file almost certainly lands mid-record, so the first line is a
    # fragment of a message rather than a message.
    if is_truncated and lines:
        lines = lines[1:]
    kept = lines[-MAX_CONSOLE_TAIL_LINES:]
    if not kept:
        logger.info("The captured console at {} is empty; this report attaches no console", tail_path)
        return None
    return "\n".join(kept) + "\n"
