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
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr.api.connect import _build_ssh_activity_wrapper_script
from imbue.mngr.api.connect import build_attach_argv
from imbue.mngr.api.connect import build_ssh_base_args
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary

_READ_CHUNK: Final[int] = 65536
# How long to wait after SIGHUP before escalating to SIGKILL on teardown.
_TERM_GRACE_SECONDS: Final[float] = 2.0
# select() timeout on the pty read loop, so the reader notices shutdown promptly.
_SELECT_TIMEOUT_SECONDS: Final[float] = 0.5

# SSH ControlMaster: the FIRST direct terminal open to a host makes a persistent
# master socket; subsequent opens (and host shells) within ControlPersist reuse
# it, so the handshake is paid once. %C is a short per-connection hash.
_CTL_DIR: Final[Path] = Path.home() / ".mngr" / "foreman-ctl"
_CONTROL_PERSIST: Final[str] = "10m"
# Bound the keepalive's ControlMaster pre-warm so a slow/unreachable host can't
# wedge the warm-pool tick that spawns it.
_CONTROL_PREWARM_TIMEOUT_SECONDS: Final[float] = 10.0


def _control_master_opts() -> list[str]:
    try:
        _CTL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001 - a missing ctl dir just disables multiplexing
        logger.trace("could not create ssh control dir {}: {}", _CTL_DIR, e)
        return []
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={_CTL_DIR}/%C",
        "-o",
        f"ControlPersist={_CONTROL_PERSIST}",
    ]


def _child_env() -> dict[str, str]:
    """Base env for a forked child: no TMUX (a foreman server inside tmux must not
    nest-attach) and a default TERM."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.setdefault("TERM", "xterm-256color")
    return env


def _ssh_env() -> dict[str, str]:
    env = _child_env()
    # kitty's TERM isn't known on remote hosts; fall back like mngr connect does.
    if env.get("TERM") == "xterm-kitty":
        env["TERM"] = "xterm-256color"
    return env


def build_agent_terminal_argv(pool: ConnectionPool, agent_name: str) -> tuple[list[str], dict[str, str]] | None:
    """Build the argv to attach to ``agent_name``'s tmux, direct (no `mngr connect`).

    Reuses mngr's own ssh/attach builders off the *pooled* (warm) host, so we skip
    the ~11s ``mngr connect`` python startup + discovery. Returns ``(argv, env)`` or
    ``None`` on any failure (caller falls back to spawning ``mngr connect``).
    """
    attach_args = pool.mngr_ctx.config.tmux.attach_args

    def _build(agent: AgentInterface, host: OnlineHostInterface) -> list[str]:
        session_name = agent.session_name
        if host.is_local:
            # Plain local attach -- no ssh at all.
            return build_attach_argv(TmuxSessionTarget(session_name=session_name), attach_args)
        ssh_args = build_ssh_base_args(host)
        ssh_args[1:1] = _control_master_opts()  # after "ssh", before user@host
        wrapper = _build_ssh_activity_wrapper_script(session_name, host.host_dir, attach_args)
        ssh_args.extend(["-t", "bash -c " + shlex.quote(wrapper)])
        return ssh_args

    try:
        argv = pool.run_on_host(agent_name, _build)
    except Exception as e:  # noqa: BLE001 - any failure -> fall back to mngr connect
        logger.info("direct-ssh terminal build failed for {} (falling back to mngr connect): {}", agent_name, e)
        return None
    return argv, _ssh_env()


def prewarm_agent_control_master(pool: ConnectionPool, agent_name: str) -> None:
    """Open or refresh the ssh ControlMaster socket the agent terminal reuses.

    The terminal attaches over a *system-ssh* ControlMaster socket, which the pool's
    paramiko keepalive does not touch. The warm pool calls this each tick so the
    master socket is already live: the first terminal open then reuses it instead of
    paying the ssh handshake, making it as fast as every later open. A no-op for
    local hosts (no ssh), and best-effort -- if it fails, the next terminal open
    just opens the master itself.
    """

    def _build(_agent: AgentInterface, host: OnlineHostInterface) -> list[str] | None:
        if host.is_local:
            return None
        ssh_args = build_ssh_base_args(host)
        ssh_args[1:1] = _control_master_opts()  # after "ssh", before user@host
        ssh_args.append("true")  # cheapest possible remote command; opens the master
        return ssh_args

    try:
        argv = pool.run_on_host(agent_name, _build)
    except Exception as e:  # noqa: BLE001 - best effort; a terminal open will warm it otherwise
        logger.trace("control-master prewarm build failed for {}: {}", agent_name, e)
        return
    if argv is None:
        return
    try:
        # Direct ssh (not a ConcurrencyGroup) so it opens the SAME multiplexing master
        # that handle_terminal_ws's ssh subprocess reuses; bounded so a hung host
        # can't wedge the keepalive tick.
        subprocess.run(  # noqa: S603 - fixed ssh argv built from the resolved host
            argv,
            env=_ssh_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CONTROL_PREWARM_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.trace("control-master prewarm ssh failed for {}: {}", agent_name, e)


def build_host_shell_argv(pool: ConnectionPool, agent_on_host: str) -> tuple[list[str], dict[str, str]] | None:
    """Build the argv for a plain login shell on the host of ``agent_on_host``.

    A VS-Code-Remote-style shell on the machine itself (no tmux, no agent). Rides
    the same warm ControlMaster socket as the agent terminals. Returns ``(argv,
    env)`` or ``None`` on failure.
    """

    def _build(_agent: AgentInterface, host: OnlineHostInterface) -> list[str]:
        if host.is_local:
            return ["bash", "-l"]
        ssh_args = build_ssh_base_args(host)
        ssh_args[1:1] = _control_master_opts()
        ssh_args.extend(["-t", "bash -l"])
        return ssh_args

    try:
        argv = pool.run_on_host(agent_on_host, _build)
    except Exception as e:  # noqa: BLE001
        logger.info("host-shell build failed via agent {}: {}", agent_on_host, e)
        return None
    return argv, _ssh_env()


def _spawn_argv_pty(argv: list[str], env: dict[str, str]) -> tuple[int, int]:
    """Fork a child running ``argv`` on a fresh pty. Returns ``(child_pid, master_fd)``."""
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        # --- child ---
        try:
            os.execvpe(argv[0], argv, env)
        except OSError:
            os._exit(127)
    return child_pid, master_fd


def _spawn_connect_pty(agent_name: str) -> tuple[int, int]:
    """Fork a child running ``mngr connect <agent_name>`` on a fresh pty.

    Returns ``(child_pid, master_fd)``. In the child this never returns -- it
    ``execvpe``s into ``mngr`` (or ``_exit``s if exec fails).
    """
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        # --- child ---
        env = _child_env()
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


def _spawn_bash_pty() -> tuple[int, int]:
    """Fork a child running a login shell (``bash -l``) on a fresh pty.

    Used by the orchestrator terminal -- a plain shell on the foreman server
    machine itself (no mngr, no agent). Returns ``(child_pid, master_fd)``.
    """
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        # --- child ---
        env = _child_env()
        try:
            os.execvpe("bash", ["bash", "-l"], env)
        except OSError:
            os._exit(127)
    return child_pid, master_fd


def _bridge_pty_to_ws(ws: Any, child_pid: int, master_fd: int, label: str) -> None:
    """Bridge a websocket to an already-spawned pty until either side closes.

    A reader thread pumps pty output -> websocket; this thread pumps websocket
    input -> pty and handles resize control messages. Binary WS frames are raw
    keystrokes; text frames are JSON control messages.
    """
    stop = threading.Event()

    def _pump_pty_to_ws() -> None:
        # poll() rather than select.select(): select() raises "filedescriptor out
        # of range in select()" the moment master_fd >= FD_SETSIZE (1024), so on a
        # busy server (many open fds) every new terminal's pty fd lands above the
        # limit and the reader dies instantly -> [disconnected]. poll() has no such
        # cap.
        poller = select.poll()
        poller.register(master_fd, select.POLLIN)
        timeout_ms = int(_SELECT_TIMEOUT_SECONDS * 1000)
        try:
            while not stop.is_set():
                try:
                    if not poller.poll(timeout_ms):
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

    reader = threading.Thread(target=_pump_pty_to_ws, name=f"foreman-term-{label}", daemon=True)
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


def handle_terminal_ws(ws: Any, agent_name: str, pool: ConnectionPool) -> None:
    """Bridge a websocket to the agent's tmux until either closes.

    Fast path: build the ssh/tmux argv in-process off the warm pool and exec ssh
    directly (skips the ~11s ``mngr connect`` python startup; ControlMaster makes
    repeat opens instant). Falls back to spawning ``mngr connect`` if the build
    fails, so the terminal never breaks.
    """
    built = build_agent_terminal_argv(pool, agent_name)
    if built is not None:
        logger.info("Opening terminal to agent {} (direct ssh)", agent_name)
        argv, env = built
        child_pid, master_fd = _spawn_argv_pty(argv, env)
    else:
        logger.info("Opening terminal to agent {} (mngr connect fallback)", agent_name)
        child_pid, master_fd = _spawn_connect_pty(agent_name)
    _bridge_pty_to_ws(ws, child_pid, master_fd, label=agent_name)
    logger.info("Closed terminal to agent {}", agent_name)


def handle_host_shell_ws(ws: Any, agent_on_host: str, host_label: str, pool: ConnectionPool) -> None:
    """Bridge a websocket to a plain login shell on a known host (VS-Code-style).

    Resolves the host via any agent on it (``agent_on_host``) and execs
    ``ssh -t … bash -l`` directly (local host -> plain ``bash -l``). No tmux, no
    agent. There is no ``mngr connect`` equivalent, so a build failure ends the
    session.
    """
    built = build_host_shell_argv(pool, agent_on_host)
    if built is None:
        logger.info("Host shell to {} unavailable (could not build ssh argv)", host_label)
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        return
    logger.info("Opening host shell to {}", host_label)
    argv, env = built
    child_pid, master_fd = _spawn_argv_pty(argv, env)
    _bridge_pty_to_ws(ws, child_pid, master_fd, label=f"shell:{host_label}")
    logger.info("Closed host shell to {}", host_label)


def handle_orchestrator_ws(ws: Any) -> None:
    """Bridge a websocket to a plain ``bash -l`` pty on the foreman server host."""
    logger.info("Opening orchestrator terminal (bash -l)")
    child_pid, master_fd = _spawn_bash_pty()
    _bridge_pty_to_ws(ws, child_pid, master_fd, label="orchestrator")
    logger.info("Closed orchestrator terminal")


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
