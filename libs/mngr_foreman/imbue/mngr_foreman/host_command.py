"""Run foreman's per-agent shell commands over SSH ControlMaster -- no paramiko.

This replaces the old warm paramiko connection pool. The pool kept a persistent
paramiko SSH connection per agent (one background reader thread each) and pinged it
every 10s; a flaky/slow moment made it panic-reconnect, and the old connection's
reader thread failed to die -> the thread leak that took the box down.

Instead we do what the *terminal* already does and has always been robust: reach a
host over the system ``ssh`` binary with **ControlMaster** multiplexing. The first
command to a host opens one persistent master socket (managed by ssh itself, kept
alive by ``ServerAliveInterval`` + ``ControlPersist``); every later command reuses
it over that socket -- no handshake, no new connection, and crucially **no Python
thread per connection**, so there is nothing to leak. When a connection truly dies,
ssh tears it down cleanly.

Speed is preserved by caching the *resolution* (agent -> host ssh args), which is the
slow (~3s) part -- exactly what the pool cached. We resolve once, extract the ssh
config, immediately drop the connection resolution opened (we only ever needed the
config), and cache the args. Every command after that is a warm ControlMaster ssh.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.connect import build_ssh_base_args
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.config.data_types import MngrContext

# SSH ControlMaster multiplexing (identical to the terminal path): first command opens
# a persistent master socket; the rest reuse it. %C is a per-connection hash.
_CTL_DIR: Final[Path] = Path.home() / ".mngr" / "foreman-ctl"
_CONTROL_PERSIST: Final[str] = "10m"
# Keep the master healthy so a silently-dropped peer is noticed and the socket closed
# cleanly by ssh itself (the robust, protocol-level keepalive -- not an app-level ping).
_SERVER_ALIVE_INTERVAL: Final[str] = "15"
_SERVER_ALIVE_COUNT_MAX: Final[str] = "3"


def control_master_opts() -> list[str]:
    """ssh options that open/reuse a persistent, self-healing master socket."""
    try:
        _CTL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001 - a missing ctl dir just disables multiplexing
        logger.trace("could not create ssh control dir {}: {}", _CTL_DIR, e)
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_CTL_DIR}/%C",
        "-o", f"ControlPersist={_CONTROL_PERSIST}",
        "-o", f"ServerAliveInterval={_SERVER_ALIVE_INTERVAL}",
        "-o", f"ServerAliveCountMax={_SERVER_ALIVE_COUNT_MAX}",
        # Never prompt; a wedged auth must fail fast, not hang the op.
        "-o", "BatchMode=yes",
    ]


@dataclass(frozen=True)
class AgentTarget:
    """Everything needed to run a command on an agent's host -- no live connection."""

    is_local: bool
    ssh_base_args: tuple[str, ...]  # e.g. ("ssh","-i",key,"-p",port,"user@host"); () for local
    session_name: str
    host_dir: Path


@dataclass
class CommandResult:
    ok: bool
    stdout: bytes
    stderr: bytes


class HostConnectionError(Exception):
    """Raised when a command over ControlMaster fails (spawn/timeout/nonzero)."""


class ControlMasterRunner:
    """Resolves an agent to its host (cached) and runs commands over ControlMaster ssh.

    Thread-safe. No paramiko, no background threads, nothing to leak. A command failure
    invalidates the cached resolution so the next call re-resolves (handles an agent
    that moved hosts or a container that restarted).
    """

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self.mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        self._targets: dict[str, AgentTarget] = {}

    def invalidate(self, agent_name: str) -> None:
        with self._lock:
            self._targets.pop(agent_name, None)

    def target(self, agent_name: str) -> AgentTarget:
        """Resolve (cached) to the agent's host ssh config. Resolution opens a paramiko
        connection once; we extract the config and drop it immediately -- we only run
        over ControlMaster after this, so nothing persistent is held."""
        with self._lock:
            hit = self._targets.get(agent_name)
        if hit is not None:
            return hit
        host_ref, agent_ref = find_one_agent(parse_agent_address(agent_name), self.mngr_ctx)
        agent, host = resolve_to_started_host_and_agent(
            host_ref=host_ref, agent_ref=agent_ref, allow_auto_start=False, mngr_ctx=self.mngr_ctx
        )
        try:
            if host.is_local:
                target = AgentTarget(True, (), agent.session_name, host.host_dir)
            else:
                target = AgentTarget(False, tuple(build_ssh_base_args(host)), agent.session_name, host.host_dir)
        finally:
            # Drop the connection resolution opened -- we cached the config; from here on
            # every op goes over ControlMaster, so no paramiko connection is kept.
            try:
                host.disconnect()  # ty: ignore[possibly-unbound-attribute]
            except Exception as e:  # noqa: BLE001 - best effort; local hosts have nothing to close
                logger.trace("post-resolve disconnect for {} (ignored): {}", agent_name, e)
        with self._lock:
            self._targets[agent_name] = target
        return target

    def _argv(self, target: AgentTarget, remote_command: str) -> list[str]:
        if target.is_local:
            return ["bash", "-c", remote_command]
        base = list(target.ssh_base_args)
        # control-master opts go after "ssh", before user@host (mirrors the terminal path)
        return base[:1] + control_master_opts() + base[1:] + [remote_command]

    def run(
        self, agent_name: str, remote_command: str, timeout_seconds: float, stdin: bytes | None = None
    ) -> CommandResult:
        """Run ``remote_command`` on the agent's host over ControlMaster (or locally).

        Binary-safe (bytes in/out). On any failure the resolution cache for this agent
        is invalidated so the next call re-resolves. Raises HostConnectionError on
        spawn/timeout failure; a nonzero exit is returned as ``ok=False`` with output.
        """
        target = self.target(agent_name)
        argv = self._argv(target, remote_command)
        try:
            proc = subprocess.run(  # noqa: S603 - argv is a fixed ssh command built from resolved host config
                argv,
                input=stdin,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as e:
            self.invalidate(agent_name)
            raise HostConnectionError(f"command on {agent_name!r} failed: {e}") from e
        return CommandResult(ok=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)
