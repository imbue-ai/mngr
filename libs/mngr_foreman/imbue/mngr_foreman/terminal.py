"""pty <-> websocket terminal bridge for the foreman web terminal page.

Spawns ``mngr connect <agent>`` under ``pty.fork()`` and bridges its terminal to
a browser websocket (xterm.js). This is how ``/login``, permission prompts, and
other TUI interactions (which the message/transcript path cannot drive) are
handled from any device.

Protocol on the websocket:
- **binary frames**  -> raw keystrokes, written straight to the pty master.
- **text frames**    -> a JSON control message. Only ``{"type":"resize",
  "cols":C,"rows":R}`` is understood; it sets the pty window size
  (``TIOCSWINSZ``) and sends ``SIGWINCH`` so the child reflows.
- terminal output read from the pty master is sent back as **binary frames**.

``mngr connect`` is a plain interactive program (``ssh -t … tmux attach`` for a
remote agent, ``tmux attach`` for a local one). ``TMUX`` is stripped from the
child env so a foreman server running inside tmux does not trip
``NestedTmuxError`` (connect.py). Closing the socket SIGHUPs the child, then
SIGKILLs after a short grace and reaps it -- this only detaches the tmux client,
so the agent itself keeps running.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary

_READ_CHUNK: Final[int] = 65536
# How long to wait after SIGHUP before escalating to SIGKILL on teardown.
_TERM_GRACE_SECONDS: Final[float] = 2.0
# select() timeout on the pty read loop, so the reader notices shutdown promptly.
_SELECT_TIMEOUT_SECONDS: Final[float] = 0.5


def _spawn_connect_pty(agent_name: str) -> tuple[int, int]:
    """Fork a child running ``mngr connect <agent_name>`` on a fresh pty.

    Returns ``(child_pid, master_fd)``. In the child this never returns -- it
    ``execvpe``s into ``mngr`` (or ``_exit``s if exec fails).
    """
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        # --- child ---
        env = dict(os.environ)
        # Strip TMUX so `tmux attach` does not refuse a nested attach.
        env.pop("TMUX", None)
        env.setdefault("TERM", "xterm-256color")
        mngr_binary = resolve_mngr_binary()
        try:
            os.execvpe(mngr_binary, [mngr_binary, "connect", agent_name], env)
        except OSError:
            # exec failed -- exit the child hard so the parent sees EOF.
            os._exit(127)
    return child_pid, master_fd


def _set_winsize(master_fd: int, child_pid: int, cols: int, rows: int) -> None:
    """Apply a new terminal size to the pty and nudge the child to reflow."""
    if cols <= 0 or rows <= 0:
        return
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        # TIOCSWINSZ already signals the foreground group, but send SIGWINCH
        # explicitly too (belt-and-suspenders; some programs poll on it).
        os.kill(child_pid, signal.SIGWINCH)
    except (OSError, ProcessLookupError) as e:
        logger.trace("Failed to set winsize: {}", e)


def _teardown(child_pid: int, master_fd: int) -> None:
    """SIGHUP the child, escalate to SIGKILL after a grace period, then reap it."""
    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        os.kill(child_pid, signal.SIGHUP)
    except ProcessLookupError:
        return
    except OSError as e:
        logger.trace("SIGHUP failed: {}", e)

    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            reaped, _ = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == child_pid:
            return
        time.sleep(0.05)

    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass


def handle_terminal_ws(ws: Any, agent_name: str) -> None:
    """Bridge a websocket to a ``mngr connect <agent_name>`` pty until either closes.

    ``ws`` is a flask-sock/simple-websocket connection (``receive``/``send``/
    ``close``). Runs in the request's own thread (threaded werkzeug). A reader
    thread pumps pty output -> websocket; this thread pumps websocket input ->
    pty and handles resize control messages.
    """
    logger.info("Opening terminal to agent {}", agent_name)
    child_pid, master_fd = _spawn_connect_pty(agent_name)
    stop = threading.Event()

    def _pump_pty_to_ws() -> None:
        try:
            while not stop.is_set():
                try:
                    ready, _, _ = select.select([master_fd], [], [], _SELECT_TIMEOUT_SECONDS)
                    if not ready:
                        continue
                    data = os.read(master_fd, _READ_CHUNK)
                except OSError:
                    break  # pty closed (child exited, or fd closed on teardown)
                if not data:
                    break
                try:
                    ws.send(data)
                except Exception:  # noqa: BLE001 - client went away; end the session
                    break
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass

    reader = threading.Thread(target=_pump_pty_to_ws, name=f"foreman-term-{agent_name}", daemon=True)
    reader.start()

    try:
        while not stop.is_set():
            message = ws.receive()
            if message is None:
                break  # client closed
            if isinstance(message, str):
                _handle_control_message(message, master_fd, child_pid)
            else:
                try:
                    os.write(master_fd, message)
                except OSError:
                    break
    except Exception as e:  # noqa: BLE001 - any ws error (incl. client close) ends the session
        logger.trace("Terminal websocket ended: {}", e)
    finally:
        # Stop the reader and let it exit its select() (<= _SELECT_TIMEOUT_SECONDS)
        # BEFORE teardown closes master_fd, so the fd is never closed out from
        # under a concurrent select()/read().
        stop.set()
        reader.join(timeout=_SELECT_TIMEOUT_SECONDS + 1.0)
        _teardown(child_pid, master_fd)
        logger.info("Closed terminal to agent {}", agent_name)


def _handle_control_message(message: str, master_fd: int, child_pid: int) -> None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") == "resize":
        cols = payload.get("cols")
        rows = payload.get("rows")
        if isinstance(cols, int) and isinstance(rows, int):
            _set_winsize(master_fd, child_pid, cols, rows)
