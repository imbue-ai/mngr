"""Per-agent Latchkey gateway processes for locally-reachable agents.

For each workspace agent running on the local machine or in a container/VM
on the local host (providers: ``local``, ``docker``, ``lima``), the desktop
client spawns a dedicated ``latchkey gateway`` subprocess bound to a free
port on 127.0.0.1. Cloud/VPS providers (``modal``, ``vultr``, ...) do not
get a gateway since agents there cannot reach the local machine directly.

Gateways are intentionally *detached* from the desktop client: once spawned
they keep running even if the desktop client exits. On the next desktop-client
launch we reconcile persisted state with the set of live processes and the
set of currently known agents:

- Persisted records whose subprocess is still running and still matches
  our expected command line + port are adopted as-is.
- Persisted records whose subprocess is dead are dropped.
- After the initial ``mngr observe`` snapshot has arrived, gateways whose
  agent is no longer discovered are terminated and their records deleted.

Wiring the gateway's address into the agent's environment (so the agent
actually uses it) happens in a follow-up task.
"""

import shutil
import socket
import sys
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey_gateway_store import LatchkeyGatewayRecord
from imbue.minds.desktop_client.latchkey_gateway_store import delete_gateway_record
from imbue.minds.desktop_client.latchkey_gateway_store import gateway_log_path
from imbue.minds.desktop_client.latchkey_gateway_store import list_gateway_records
from imbue.minds.desktop_client.latchkey_gateway_store import save_gateway_record
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.mngr.primitives import AgentId

LATCHKEY_BINARY: Final[str] = "latchkey"

_DEFAULT_LISTEN_HOST: Final[str] = "127.0.0.1"

_LAUNCHER_MODULE: Final[str] = "imbue.minds.desktop_client._latchkey_gateway_launcher"

_LAUNCHER_TIMEOUT_SECONDS: Final[float] = 15.0

_LIVENESS_CONNECT_TIMEOUT_SECONDS: Final[float] = 1.0

_TERMINATE_GRACE_SECONDS: Final[float] = 5.0

# Providers whose agents run on the local machine or can easily reach it
# (containers and VMs on the same host). Cloud/VPS providers (``modal``,
# ``vultr``, ...) are excluded because agents there cannot reach 127.0.0.1
# on this machine.
_LOCAL_REACHABLE_PROVIDER_NAMES: Final[frozenset[str]] = frozenset({"local", "docker", "lima"})


class LatchkeyGatewayError(Exception):
    """Raised when a Latchkey gateway subprocess cannot be managed."""


class LatchkeyBinaryNotFoundError(LatchkeyGatewayError, FileNotFoundError):
    """Raised when the ``latchkey`` binary is not available on PATH."""


class LatchkeyGatewayManagerNotStartedError(LatchkeyGatewayError, RuntimeError):
    """Raised when the manager is used before ``start()`` has been called."""


class LatchkeyGatewayInfo(FrozenModel):
    """Metadata for a running per-agent Latchkey gateway."""

    agent_id: AgentId = Field(description="The agent this gateway is dedicated to")
    host: str = Field(description="Host the gateway is listening on (typically 127.0.0.1)")
    port: int = Field(description="Port the gateway is listening on")
    pid: int = Field(description="PID of the ``latchkey gateway`` process")


def is_local_reachable_provider(provider_name: str) -> bool:
    """Return True iff agents on this provider can reach the local machine.

    Covers the local machine itself (``local``) and provider types that
    create containers or VMs on the local host (``docker``, ``lima``).
    Cloud/VPS providers (``modal``, ``vultr``, ...) are excluded.
    """
    return provider_name in _LOCAL_REACHABLE_PROVIDER_NAMES


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


def _is_record_alive(record: LatchkeyGatewayRecord) -> bool:
    """Verify that a persisted record still corresponds to our running gateway.

    Three checks, all must pass:
    1. A process with the recorded PID exists.
    2. That process's cmdline looks like ``latchkey gateway`` (not PID reuse).
    3. Something accepts TCP connections on the recorded host:port.
    """
    try:
        process = psutil.Process(record.pid)
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logger.debug("Latchkey record for {} is stale (pid={}): {}", record.agent_id, record.pid, e)
        return False
    if not _cmdline_looks_like_latchkey_gateway(cmdline):
        logger.debug(
            "Latchkey record for {} points at pid {} whose cmdline is not ours: {!r}",
            record.agent_id,
            record.pid,
            cmdline,
        )
        return False
    if not _is_port_listening(record.host, record.port):
        logger.debug(
            "Latchkey record for {} points at pid {} but {}:{} is not accepting connections",
            record.agent_id,
            record.pid,
            record.host,
            record.port,
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


def _build_info_from_record(record: LatchkeyGatewayRecord) -> LatchkeyGatewayInfo:
    return LatchkeyGatewayInfo(
        agent_id=record.agent_id,
        host=record.host,
        port=record.port,
        pid=record.pid,
    )


class LatchkeyGatewayManager(MutableModel):
    """Spawns, adopts, and tracks per-agent ``latchkey gateway`` subprocesses.

    Gateways are intentionally spawned with ``start_new_session=True`` via the
    ``_latchkey_gateway_launcher`` helper so they survive desktop-client
    restarts. Lifecycle is reconciled against persisted records on start.
    """

    latchkey_binary: str = Field(default=LATCHKEY_BINARY, frozen=True, description="Path to latchkey binary")
    listen_host: str = Field(
        default=_DEFAULT_LISTEN_HOST,
        frozen=True,
        description="Host to bind each spawned gateway to",
    )
    launcher_python: str = Field(
        default_factory=lambda: sys.executable,
        frozen=True,
        description="Python interpreter used to invoke the detached launcher module",
    )

    _data_dir: Path | None = PrivateAttr(default=None)
    _infos: dict[str, LatchkeyGatewayInfo] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_started: bool = PrivateAttr(default=False)

    def start(self, data_dir: Path) -> None:
        """Load persisted records from ``data_dir``, adopting still-alive gateways.

        Dead records are removed from disk. Gateways that are still running
        and still look like ours are tracked internally so subsequent calls
        to ``ensure_gateway_started`` are no-ops for those agents.
        """
        with self._lock:
            if self._is_started:
                return
            self._data_dir = data_dir
            self._infos.clear()
            for record in list_gateway_records(data_dir):
                if _is_record_alive(record):
                    logger.info(
                        "Adopted existing latchkey gateway for agent {} (pid={}, {}:{})",
                        record.agent_id,
                        record.pid,
                        record.host,
                        record.port,
                    )
                    self._infos[str(record.agent_id)] = _build_info_from_record(record)
                else:
                    logger.info(
                        "Discarding stale latchkey gateway record for agent {} (pid={})",
                        record.agent_id,
                        record.pid,
                    )
                    delete_gateway_record(data_dir, record.agent_id)
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
        """
        aid_str = str(agent_id)
        with self._lock:
            data_dir = self._require_started_locked()
            existing = self._infos.get(aid_str)
        if existing is not None and self._verify_info_alive(existing):
            return existing
        # Stale entry -- fall through and replace.
        info = self._spawn_gateway(agent_id, data_dir)
        with self._lock:
            self._infos[aid_str] = info
        return info

    def stop_gateway_for_agent(self, agent_id: AgentId) -> None:
        """Terminate the gateway for ``agent_id`` and delete its record."""
        aid_str = str(agent_id)
        with self._lock:
            data_dir = self._data_dir
            info = self._infos.pop(aid_str, None)
        if info is not None:
            logger.info("Stopping latchkey gateway for agent {} (pid={})", agent_id, info.pid)
            _terminate_pid(info.pid)
        if data_dir is not None:
            delete_gateway_record(data_dir, agent_id)

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
            logger.info("Reconciling: agent {} no longer known; terminating its latchkey gateway", aid_str)
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

    def _verify_info_alive(self, info: LatchkeyGatewayInfo) -> bool:
        """Re-verify a tracked info's liveness using the same checks as adoption."""
        record = LatchkeyGatewayRecord(
            agent_id=info.agent_id,
            host=info.host,
            port=info.port,
            pid=info.pid,
            started_at=datetime.now(timezone.utc),
        )
        return _is_record_alive(record)

    def _spawn_gateway(self, agent_id: AgentId, data_dir: Path) -> LatchkeyGatewayInfo:
        if shutil.which(self.latchkey_binary) is None and not Path(self.latchkey_binary).is_file():
            raise LatchkeyBinaryNotFoundError(f"latchkey binary not found: {self.latchkey_binary}")

        port = _allocate_free_port(self.listen_host)
        log_path = gateway_log_path(data_dir, agent_id)
        pid_file = log_path.with_suffix(".pid")
        # Clear any stale pid file so we can tell whether the launcher wrote one.
        if pid_file.exists():
            pid_file.unlink()
        command = [
            self.launcher_python,
            "-m",
            _LAUNCHER_MODULE,
            "--latchkey-binary",
            self.latchkey_binary,
            "--listen-host",
            self.listen_host,
            "--listen-port",
            str(port),
            "--log-path",
            str(log_path),
            "--pid-file",
            str(pid_file),
        ]

        with log_span(
            "Starting latchkey gateway for agent {} on {}:{}",
            agent_id,
            self.listen_host,
            port,
        ):
            pid = _run_launcher_and_read_pid(command, pid_file)

        record = LatchkeyGatewayRecord(
            agent_id=agent_id,
            host=self.listen_host,
            port=port,
            pid=pid,
            started_at=datetime.now(timezone.utc),
        )
        save_gateway_record(data_dir, record)
        return _build_info_from_record(record)


def _run_launcher_and_read_pid(command: list[str], pid_file: Path) -> int:
    """Invoke the detached launcher script and read the child PID from ``pid_file``.

    A short-lived ``ConcurrencyGroup`` is created per invocation because the
    launcher exits immediately after spawning its detached child -- we do not
    need (or want) long-running process tracking here.
    """
    cg = ConcurrencyGroup(name="latchkey-gateway-launcher")
    try:
        with cg:
            cg.run_process_to_completion(
                command=command,
                timeout=_LAUNCHER_TIMEOUT_SECONDS,
                is_checked_after=True,
            )
    except ProcessSetupError as e:
        raise LatchkeyGatewayError(f"Failed to invoke latchkey gateway launcher: {e}") from e
    except ProcessError as e:
        raise LatchkeyGatewayError(f"Latchkey gateway launcher failed: {e}") from e
    if not pid_file.is_file():
        raise LatchkeyGatewayError(f"Latchkey gateway launcher did not write PID file at {pid_file}")
    try:
        pid_text = pid_file.read_text().strip()
        return int(pid_text)
    except (OSError, ValueError) as e:
        raise LatchkeyGatewayError(
            f"Latchkey gateway launcher produced an unreadable PID file at {pid_file}"
        ) from e


class LatchkeyGatewayDiscoveryHandler(FrozenModel):
    """Discovery callback that spawns a Latchkey gateway for each local-reachable agent.

    Intended to be registered via ``MngrStreamManager.add_on_agent_discovered_callback``.
    Ignores agents whose provider is not local-reachable (cloud/VPS).
    """

    gateway_manager: LatchkeyGatewayManager = Field(description="Manager that owns the gateway subprocesses")

    def __call__(self, agent_id: AgentId, ssh_info: RemoteSSHInfo | None, provider_name: str) -> None:
        del ssh_info
        if not is_local_reachable_provider(provider_name):
            logger.trace(
                "Skipping latchkey gateway for agent {} on non-local provider {!r}",
                agent_id,
                provider_name,
            )
            return
        try:
            self.gateway_manager.ensure_gateway_started(agent_id)
        except LatchkeyGatewayError as e:
            logger.warning("Failed to start latchkey gateway for agent {}: {}", agent_id, e)


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
