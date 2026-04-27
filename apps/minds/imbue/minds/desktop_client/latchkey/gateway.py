"""Per-agent Latchkey gateway processes for locally-reachable agents.

For each workspace agent, the desktop client spawns a dedicated ``latchkey
gateway`` subprocess bound to a free port on 127.0.0.1. Outside of the `dev`
launch mode, gateway is exposed to the agent via an SSH reverse tunnel.

Gateways are intentionally *detached* from the desktop client: once spawned
they keep running even if the desktop client exits. On the next desktop-client
launch we reconcile persisted state with the set of live processes and the
set of currently known agents:

- Persisted records whose subprocess is still running and still matches
  our expected command line + port are adopted as-is.
- Persisted records whose subprocess is dead are dropped.
- After the initial ``mngr observe`` snapshot has arrived, gateways whose
  agent is no longer discovered are terminated and their records deleted.
"""

import shutil
import socket
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import paramiko
import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.minds.desktop_client.latchkey._spawn import spawn_detached_latchkey_gateway
from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.latchkey.store import delete_gateway_info
from imbue.minds.desktop_client.latchkey.store import ensure_browser_log_path
from imbue.minds.desktop_client.latchkey.store import gateway_log_path
from imbue.minds.desktop_client.latchkey.store import list_gateway_infos
from imbue.minds.desktop_client.latchkey.store import save_gateway_info
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.mngr.primitives import AgentId

LATCHKEY_BINARY: Final[str] = "latchkey"

_DEFAULT_LISTEN_HOST: Final[str] = "127.0.0.1"

_LIVENESS_CONNECT_TIMEOUT_SECONDS: Final[float] = 1.0

_TERMINATE_GRACE_SECONDS: Final[float] = 5.0

# Fixed port that every containerized/VM/VPS agent sees on its own 127.0.0.1
# when reaching the Latchkey gateway. A per-agent SSH reverse tunnel bridges
# this to the dynamic per-agent gateway port on the desktop host, so the
# ``LATCHKEY_GATEWAY`` env var injected at ``mngr create`` time can be the
# same constant URL for every agent. Matches the documented default of the
# upstream ``latchkey gateway`` CLI (``1989``).
AGENT_SIDE_LATCHKEY_PORT: Final[int] = 1989


class LatchkeyGatewayError(Exception):
    """Raised when a Latchkey gateway subprocess cannot be managed."""


class LatchkeyBinaryNotFoundError(LatchkeyGatewayError, FileNotFoundError):
    """Raised when the ``latchkey`` binary is not available on PATH."""


class LatchkeyGatewayManagerNotStartedError(LatchkeyGatewayError, RuntimeError):
    """Raised when the manager is used before ``start()`` has been called."""


def _allocate_free_port(host: str) -> int:
    """Pick a free TCP port on ``host`` by binding to port 0 and reading it back.

    There is an inherent TOCTOU race: the chosen port could be claimed by
    another process between the time this function returns and the time
    ``latchkey gateway`` rebinds it. In practice the window is tiny and
    the desktop client is the only interested party on 127.0.0.1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _cmdline_looks_like_latchkey_gateway(cmdline: list[str]) -> bool:
    """Check whether a process's ``cmdline`` looks like our ``latchkey gateway``.

    We require ``latchkey`` to appear as a path component anywhere in the
    argv (to tolerate shebang rewriting that injects ``env`` / ``python`` as
    argv[0]) and the literal ``gateway`` subcommand anywhere after it. This
    guards against PID reuse: an unrelated process that happens to grab the
    same PID almost certainly won't match.
    """
    if not cmdline:
        return False
    latchkey_idx: int | None = None
    for idx, arg in enumerate(cmdline):
        # Match ``latchkey`` anywhere in the arg. This handles direct
        # execution (``/usr/local/bin/latchkey``), shebang rewrites that
        # push the interpreter ahead of the script path
        # (``/usr/bin/env node /opt/latchkey/cli``), and wrappers whose
        # script path includes the word "latchkey" somewhere.
        if "latchkey" in arg:
            latchkey_idx = idx
            break
    if latchkey_idx is None:
        return False
    return "gateway" in cmdline[latchkey_idx + 1 :]


def _is_port_listening(host: str, port: int) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds within the timeout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_LIVENESS_CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


def _is_info_alive(info: LatchkeyGatewayInfo) -> bool:
    """Verify that an info still corresponds to our running gateway.

    Three checks, all must pass:
    1. A process with the recorded PID exists.
    2. That process's cmdline looks like ``latchkey gateway`` (not PID reuse).
    3. Something accepts TCP connections on the recorded host:port.
    """
    try:
        process = psutil.Process(info.pid)
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logger.debug("Latchkey info for {} is stale (pid={}): {}", info.agent_id, info.pid, e)
        return False
    if not _cmdline_looks_like_latchkey_gateway(cmdline):
        logger.debug(
            "Latchkey info for {} points at pid {} whose cmdline is not ours: {!r}",
            info.agent_id,
            info.pid,
            cmdline,
        )
        return False
    if not _is_port_listening(info.host, info.port):
        logger.debug(
            "Latchkey info for {} points at pid {} but {}:{} is not accepting connections",
            info.agent_id,
            info.pid,
            info.host,
            info.port,
        )
        return False
    return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM a PID, falling back to SIGKILL after a grace period.

    Silently tolerates already-dead / inaccessible / not-ours processes.
    """
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except psutil.TimeoutExpired:
        logger.warning("Latchkey gateway pid {} did not exit within grace period; sending SIGKILL", pid)
        try:
            process.kill()
        except psutil.NoSuchProcess:
            return
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug("Could not terminate pid {}: {}", pid, e)


class LatchkeyGatewayManager(MutableModel):
    """Spawns, adopts, and tracks per-agent ``latchkey gateway`` subprocesses.

    Gateways are spawned detached (``start_new_session=True`` inside
    :func:`spawn_detached_latchkey_gateway`) so they survive desktop-client
    restarts. Lifecycle is reconciled against persisted records on start.
    """

    latchkey_binary: str = Field(default=LATCHKEY_BINARY, frozen=True, description="Path to Latchkey binary")
    listen_host: str = Field(
        default=_DEFAULT_LISTEN_HOST,
        frozen=True,
        description="Host to bind each spawned gateway to",
    )
    latchkey_directory: Path | None = Field(
        default=None,
        frozen=True,
        description=(
            "Value to pass through as ``LATCHKEY_DIRECTORY`` to each spawned gateway. "
            "When set, all minds-managed gateways share this credential/config directory "
            "instead of falling back to the default ``~/.latchkey``. When ``None``, "
            "latchkey uses its own default."
        ),
    )

    _data_dir: Path | None = PrivateAttr(default=None)
    _infos: dict[str, LatchkeyGatewayInfo] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_started: bool = PrivateAttr(default=False)
    _has_ensured_browser: bool = PrivateAttr(default=False)

    def start(self, data_dir: Path) -> None:
        """Load persisted infos from ``data_dir``, adopting still-alive gateways.

        Dead records are removed from disk. Gateways that are still running
        and still look like ours are tracked internally so subsequent calls
        to ``ensure_gateway_started`` are no-ops for those agents.

        Liveness probes include a TCP connect per info (up to
        ``_LIVENESS_CONNECT_TIMEOUT_SECONDS`` each), which is why they run
        outside the manager lock. ``start()`` is only expected to be called
        once before any concurrent use of the manager, so there is no real
        contention to worry about here.
        """
        adopted: list[LatchkeyGatewayInfo] = []
        stale: list[LatchkeyGatewayInfo] = []
        for info in list_gateway_infos(data_dir):
            if _is_info_alive(info):
                adopted.append(info)
            else:
                stale.append(info)

        with self._lock:
            if self._is_started:
                return
            self._data_dir = data_dir
            self._infos.clear()
            for info in adopted:
                logger.info(
                    "Adopted existing Latchkey gateway for agent {} (pid={}, {}:{})",
                    info.agent_id,
                    info.pid,
                    info.host,
                    info.port,
                )
                self._infos[str(info.agent_id)] = info
            for info in stale:
                logger.info(
                    "Discarding stale Latchkey gateway record for agent {} (pid={})",
                    info.agent_id,
                    info.pid,
                )
                delete_gateway_info(data_dir, info.agent_id)
            self._is_started = True

    def stop(self) -> None:
        """Release manager state without terminating running gateways.

        Gateways must survive desktop-client exit. This method only drops
        the in-memory tracking; the persisted records and the subprocesses
        themselves are left intact for the next desktop-client launch.
        """
        with self._lock:
            self._infos.clear()
            self._is_started = False

    def ensure_gateway_started(self, agent_id: AgentId) -> LatchkeyGatewayInfo:
        """Start a gateway for ``agent_id`` if one is not already running.

        Idempotent: returns the existing info when an adopted/live gateway
        is already tracked, otherwise spawns a fresh one on a newly
        allocated free port and persists a record.

        The slow steps -- liveness probe of an existing info and subprocess
        spawn -- run outside the manager lock; committing the result to
        ``_infos`` and the on-disk record is done atomically under the lock.
        """
        aid_str = str(agent_id)
        with self._lock:
            data_dir = self._require_started_locked()
            existing = self._infos.get(aid_str)
        if existing is not None and _is_info_alive(existing):
            return existing
        # Stale or absent -- spawn a replacement outside the lock.
        info = self._spawn_gateway(agent_id, data_dir)
        with self._lock:
            self._infos[aid_str] = info
            save_gateway_info(data_dir, info)
        return info

    def stop_gateway_for_agent(self, agent_id: AgentId) -> None:
        """Terminate the gateway for ``agent_id`` and delete its record.

        The in-memory entry and the on-disk record are removed atomically
        under the manager lock so no other caller can observe a half-torn-down
        state. ``_terminate_pid`` is deliberately called outside the lock
        because it can wait up to ``_TERMINATE_GRACE_SECONDS`` for the child
        to exit.
        """
        aid_str = str(agent_id)
        with self._lock:
            data_dir = self._data_dir
            info = self._infos.pop(aid_str, None)
            if info is not None and data_dir is not None:
                delete_gateway_info(data_dir, agent_id)
        if info is not None:
            logger.info("Stopping Latchkey gateway for agent {} (pid={})", agent_id, info.pid)
            _terminate_pid(info.pid)

    def reconcile_with_known_agents(self, known_agent_ids: frozenset[AgentId]) -> None:
        """Terminate gateways whose agent is no longer in ``known_agent_ids``.

        Intended to be called once after the initial ``mngr observe``
        snapshot has arrived, so we can clean up gateways that belonged to
        agents destroyed while the desktop client was not running.
        """
        with self._lock:
            if not self._is_started:
                return
            orphaned = [aid_str for aid_str in self._infos if AgentId(aid_str) not in known_agent_ids]
        for aid_str in orphaned:
            logger.info("Reconciling: agent {} no longer known; terminating its Latchkey gateway", aid_str)
            self.stop_gateway_for_agent(AgentId(aid_str))

    def get_gateway_info(self, agent_id: AgentId) -> LatchkeyGatewayInfo | None:
        """Return the gateway info for ``agent_id``, or ``None`` if no gateway is tracked."""
        with self._lock:
            return self._infos.get(str(agent_id))

    def list_gateways(self) -> tuple[LatchkeyGatewayInfo, ...]:
        """Return all currently tracked gateways."""
        with self._lock:
            return tuple(self._infos.values())

    def _require_started_locked(self) -> Path:
        if not self._is_started or self._data_dir is None:
            raise LatchkeyGatewayManagerNotStartedError(
                "LatchkeyGatewayManager.start(data_dir=...) must be called before use"
            )
        return self._data_dir

    def _spawn_gateway(self, agent_id: AgentId, data_dir: Path) -> LatchkeyGatewayInfo:
        """Build a fresh ``LatchkeyGatewayInfo`` by spawning a detached gateway.

        Does not mutate ``_infos`` or persist the info -- the caller is
        responsible for committing both under the manager lock.
        """
        if shutil.which(self.latchkey_binary) is None and not Path(self.latchkey_binary).is_file():
            raise LatchkeyBinaryNotFoundError(f"Latchkey binary not found: {self.latchkey_binary}")

        # Fire off ``latchkey ensure-browser`` in parallel the first time we
        # actually spawn something in this minds session. It runs detached
        # alongside the gateway spawn below and we don't wait for it.
        self._ensure_browser_once(data_dir)

        port = _allocate_free_port(self.listen_host)
        log_path = gateway_log_path(data_dir, agent_id)

        with log_span(
            "Starting Latchkey gateway for agent {} on {}:{}",
            agent_id,
            self.listen_host,
            port,
        ):
            try:
                pid = spawn_detached_latchkey_gateway(
                    latchkey_binary=self.latchkey_binary,
                    listen_host=self.listen_host,
                    listen_port=port,
                    log_path=log_path,
                    latchkey_directory=self.latchkey_directory,
                )
            except OSError as e:
                raise LatchkeyGatewayError(f"Failed to spawn Latchkey gateway for agent {agent_id}: {e}") from e

        return LatchkeyGatewayInfo(
            agent_id=agent_id,
            host=self.listen_host,
            port=port,
            pid=pid,
            started_at=datetime.now(timezone.utc),
        )

    def _ensure_browser_once(self, data_dir: Path) -> None:
        """Spawn ``latchkey ensure-browser`` the first time we're asked to, per manager lifetime.

        ``ensure-browser`` discovers or downloads a Playwright-compatible
        browser into the shared latchkey directory. It only needs to succeed
        once per machine, but re-running it is a cheap no-op. We call it
        once per minds session at the point we know latchkey is actually
        being used (i.e. right before spawning our first gateway), fire and
        forget. Failures here are logged but must not prevent gateway spawn.
        """
        with self._lock:
            if self._has_ensured_browser:
                return
            self._has_ensured_browser = True
        log_path = ensure_browser_log_path(data_dir)
        try:
            pid = spawn_detached_latchkey_ensure_browser(
                latchkey_binary=self.latchkey_binary,
                log_path=log_path,
                latchkey_directory=self.latchkey_directory,
            )
        except OSError as e:
            logger.warning("Failed to spawn ``latchkey ensure-browser``: {}", e)
            return
        logger.info("Spawned ``latchkey ensure-browser`` (pid={}, log={})", pid, log_path)


class LatchkeyGatewayDiscoveryHandler(FrozenModel):
    """Discovery callback that spawns a Latchkey gateway for each agent and tunnels it in.

    Intended to be registered via ``MngrStreamManager.add_on_agent_discovered_callback``.

    For every discovered agent, ensures a dedicated ``latchkey gateway`` subprocess
    is running on the desktop host. Agents that reach the desktop via SSH
    (containers, VMs, VPS) also get a reverse tunnel that exposes the host-side
    gateway on the agent's own ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. DEV-mode
    agents run on the bare host and need no tunnel; their ``LATCHKEY_GATEWAY``
    env var points directly at the dynamic host port.
    """

    gateway_manager: LatchkeyGatewayManager = Field(description="Manager that owns the gateway subprocesses")
    tunnel_manager: SSHTunnelManager = Field(
        description="SSH tunnel manager used to reverse-forward the host-side gateway into remote agents"
    )

    def __call__(self, agent_id: AgentId, ssh_info: RemoteSSHInfo | None, provider_name: str) -> None:
        del provider_name
        try:
            info = self.gateway_manager.ensure_gateway_started(agent_id)
        except LatchkeyGatewayError as e:
            logger.warning("Failed to start Latchkey gateway for agent {}: {}", agent_id, e)
            return

        if ssh_info is None:
            # DEV-mode agent runs on the bare host; it reaches the gateway
            # directly on its dynamic host port, so no tunnel is needed.
            return

        try:
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=info.port,
                remote_port=AGENT_SIDE_LATCHKEY_PORT,
            )
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.warning(
                "Failed to set up Latchkey reverse tunnel for agent {} (host-side port {}): {}",
                agent_id,
                info.port,
                e,
            )


class LatchkeyGatewayDestructionHandler(FrozenModel):
    """Discovery callback that tears down the gateway when an agent is destroyed."""

    gateway_manager: LatchkeyGatewayManager = Field(description="Manager that owns the gateway subprocesses")

    def __call__(self, agent_id: AgentId) -> None:
        self.gateway_manager.stop_gateway_for_agent(agent_id)


class LatchkeyGatewayReconcileCallback(MutableModel):
    """Resolver change callback that reconciles gateways once initial discovery completes.

    Registered against ``MngrCliBackendResolver.add_on_change_callback`` by the
    desktop client startup. Fires once (the first time ``has_completed_initial_discovery``
    returns True), terminates any gateways whose agent is no longer known, then
    unregisters itself.
    """

    gateway_manager: LatchkeyGatewayManager = Field(frozen=True, description="Manager to reconcile")
    resolver: MngrCliBackendResolver = Field(
        frozen=True, description="Backend resolver whose discovery status we watch"
    )

    _has_fired: bool = PrivateAttr(default=False)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __call__(self) -> None:
        with self._lock:
            if self._has_fired:
                return
            if not self.resolver.has_completed_initial_discovery():
                return
            self._has_fired = True
        known_agent_ids = frozenset(self.resolver.list_known_agent_ids())
        self.gateway_manager.reconcile_with_known_agents(known_agent_ids)
        self.resolver.remove_on_change_callback(self)
