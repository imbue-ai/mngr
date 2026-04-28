import json
import os
import queue
import shlex
import threading
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import Field
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.errors import EnvironmentStoppedError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds_workspace_server.agent_discovery import discover_agents
from imbue.minds_workspace_server.models import AgentCreationError
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.models import ApplicationEntry
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.utils.name_generator import generate_agent_name

_APPLICATIONS_TOML_FILENAME = "runtime/applications.toml"
_APPLICATIONS_TOML_BASENAME = "applications.toml"
_DEFAULT_MNGR_BINARY = "mngr"


_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS = 5.0


def _safe_log_put(log_queue: queue.Queue[str | None], message: str | None) -> None:
    """Non-blocking put for a creation-log queue.

    The creation thread must never block on individual log lines. If the
    WebSocket client streaming proto-agent logs disconnects mid-creation,
    nothing is draining the queue, and a blocking ``put`` would hang the
    thread at the next log line -- which in turn prevents
    ``proto_agent_completed`` from ever firing. We drop log lines on a
    full queue; callers that need delivery guarantees for sentinels
    (``done: True`` + the ``None`` terminator) should use
    :func:`_completion_signal_put` instead.
    """
    try:
        log_queue.put_nowait(message)
    except queue.Full:
        _loguru_logger.trace("Creation log queue full; dropping line")


def _completion_signal_put(log_queue: queue.Queue[str | None], message: str | None) -> None:
    """Blocking put (with timeout) for completion sentinels.

    Unlike per-line log writes, the completion sentinel + None terminator
    must reach the consumer -- otherwise ``_proto_agent_logs_endpoint``
    loops forever on ``queue.get()`` and the log WebSocket never closes.
    We therefore block briefly (bounded by
    ``_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS``) to give a slow consumer
    time to drain. If the queue is still full at the deadline, log at
    warning level and drop -- the out-of-band
    ``broadcast_proto_agent_completed`` WS broadcast is the authoritative
    signal to the main UI, so the log-channel sentinel being dropped
    only degrades the dedicated log view, not overall correctness.
    """
    try:
        log_queue.put(message, block=True, timeout=_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS)
    except queue.Full:
        _loguru_logger.warning(
            "Creation log queue full; dropping completion sentinel. "
            "The log WebSocket consumer may hang until the queue is garbage-collected."
        )


class _LogQueueCallback(MutableModel):
    """Callable that appends process output lines as JSON to a queue."""

    model_config = {"arbitrary_types_allowed": True}

    log_queue: queue.Queue[str | None] = Field(description="Queue to write log lines into")

    def __call__(self, line: str, _is_stdout: bool) -> None:
        _safe_log_put(self.log_queue, json.dumps({"line": line.rstrip("\n")}))


class _ApplicationsFileHandler(FileSystemEventHandler):
    """Watchdog handler that triggers on any change to applications.toml.

    Uses ``on_any_event`` rather than ``on_modified`` because scripts/forward_port.py
    upserts atomically via ``tempfile.mkstemp`` + ``os.replace``. Atomic replaces
    surface through watchdog as moved/created events, not modified events, so a
    handler that only overrides ``on_modified`` would silently miss every
    service registration after the watcher starts.

    Events are filtered to only those whose src or dest path basename is
    ``applications.toml``. Without this filter we'd also fire on every write
    to forward_port.py's ``applications.toml.*.tmp`` scratch files, which is
    correctness-neutral (the re-read is idempotent) but produces a broadcast
    storm per upsert.
    """

    agent_id: str
    on_change: Any

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(p) == _APPLICATIONS_TOML_BASENAME for p in paths):
            self.on_change(self.agent_id)


def _make_applications_file_handler(
    agent_id: str,
    on_change: Any,
) -> _ApplicationsFileHandler:
    """Create an applications file handler for the given agent."""
    handler = _ApplicationsFileHandler()
    handler.agent_id = agent_id
    handler.on_change = on_change
    return handler


class AgentManager:
    """Manages agent lifecycle detection, application watching, and agent creation.

    Runs mngr observe as a subprocess for event-driven agent lifecycle detection.
    Watches runtime/applications.toml for each agent.
    Handles agent creation via local mngr create calls.
    """

    _broadcaster: WebSocketBroadcaster
    _lock: threading.Lock
    _agents: dict[str, AgentStateItem]
    _applications: list[ApplicationEntry]
    _app_observers: dict[str, Any]
    _proto_agents: dict[str, dict[str, Any]]
    _log_queues: dict[str, queue.Queue[str | None]]
    _own_agent_id: str
    _own_work_dir: str
    _shutdown_event: ShutdownEvent
    _observe_cg: ConcurrencyGroup | None
    _observe_process: RunningProcess | None
    _creation_cg: ConcurrencyGroup
    _mngr_binary: str

    @classmethod
    def build(cls, broadcaster: WebSocketBroadcaster, mngr_binary: str = _DEFAULT_MNGR_BINARY) -> "AgentManager":
        """Build an AgentManager with the given broadcaster.

        ``mngr_binary`` is the path or name of the mngr executable used for
        the discovery-only observe subprocess and for agent-creation commands.
        """
        manager = cls.__new__(cls)
        manager._broadcaster = broadcaster
        manager._lock = threading.Lock()
        manager._agents = {}
        manager._applications = []
        manager._app_observers = {}
        manager._proto_agents = {}
        manager._log_queues = {}
        manager._own_agent_id = os.environ.get("MNGR_AGENT_ID", "")
        manager._own_work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
        manager._shutdown_event = ShutdownEvent.build_root()
        manager._observe_cg = None
        manager._observe_process = None
        manager._creation_cg = ConcurrencyGroup(name="agent-creation")
        manager._creation_cg.__enter__()
        manager._mngr_binary = mngr_binary
        return manager

    def start(self) -> None:
        """Start the observe subprocess and perform initial agent discovery."""
        self._initial_discover()
        self._start_observe()

    def start_without_observe(self) -> None:
        """Start with initial discovery only, no observe subprocess. For testing."""
        self._initial_discover()

    def stop(self) -> None:
        """Stop the observe subprocess, file watchers, and creation threads."""
        self._shutdown_event.set()

        if self._observe_cg is not None:
            self._observe_cg.shutdown()
            self._observe_cg.__exit__(None, None, None)
            self._observe_cg = None

        self._creation_cg.__exit__(None, None, None)

        for observer in self._app_observers.values():
            observer.stop()
        for observer in self._app_observers.values():
            observer.join(timeout=5)
        self._app_observers.clear()

    @property
    def broadcaster(self) -> WebSocketBroadcaster:
        """The WebSocketBroadcaster this manager owns. Primarily useful to
        callers that need to reuse the same broadcaster across related
        application state (e.g. the workspace_server lifespan when an
        externally-constructed AgentManager is injected for tests)."""
        return self._broadcaster

    def get_agents(self) -> list[AgentStateItem]:
        """Return current agent list."""
        with self._lock:
            return list(self._agents.values())

    def get_agent_by_id(self, agent_id: str) -> AgentStateItem | None:
        """Look up a single agent by ID."""
        with self._lock:
            return self._agents.get(agent_id)

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the tracked state and broadcast the update.

        Called after a successful mngr destroy to immediately reflect
        the destruction without waiting for the observe subprocess.
        """
        with self._lock:
            self._agents.pop(agent_id, None)

        self._stop_app_watcher(agent_id)
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def get_applications(self) -> list[ApplicationEntry]:
        """Return the primary agent's application list."""
        with self._lock:
            return list(self._applications)

    def get_applications_serialized(self) -> list[dict[str, str]]:
        """Return the primary agent's application list serialized for JSON."""
        with self._lock:
            return [{"name": app.name, "url": app.url} for app in self._applications]

    def get_service_url(self, service_name: str) -> str | None:
        """Return the local backend URL for a service, or None if it isn't registered."""
        with self._lock:
            for app in self._applications:
                if app.name == service_name:
                    return app.url
            return None

    def list_service_names(self) -> tuple[str, ...]:
        """Return the names of all currently registered services, sorted alphabetically."""
        with self._lock:
            return tuple(sorted(app.name for app in self._applications))

    def get_agents_serialized(self) -> list[dict[str, Any]]:
        """Return agent list serialized for JSON."""
        with self._lock:
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "state": a.state,
                    "labels": a.labels,
                    "work_dir": a.work_dir,
                }
                for a in self._agents.values()
            ]

    def get_proto_agents(self) -> list[dict[str, Any]]:
        """Return list of proto-agents (agents being created)."""
        with self._lock:
            return list(self._proto_agents.values())

    def get_log_queue(self, agent_id: str) -> queue.Queue[str | None] | None:
        """Get the log queue for a proto-agent creation process."""
        with self._lock:
            return self._log_queues.get(agent_id)

    def get_own_agent_id(self) -> str:
        """Return this server's own agent ID from the environment."""
        return self._own_agent_id

    def generate_random_name(self) -> str:
        """Generate a random agent name using mngr's name generator."""
        return str(generate_agent_name(AgentNameStyle.COOLNAME))

    def create_worktree_agent(self, name: str, selected_agent_id: str) -> str:
        """Create a new worktree agent. Returns the pre-generated agent ID."""
        agent_id = str(AgentId())

        with self._lock:
            work_dir = self._resolve_agent_work_dir(selected_agent_id)
            parent = self._agents.get(selected_agent_id)
            parent_labels = dict(parent.labels) if parent else {}

        if work_dir is None:
            msg = f"Cannot determine work directory for agent {selected_agent_id}"
            raise AgentCreationError(msg)

        current_branch = self._get_current_branch(Path(work_dir))
        new_branch = f"mngr/{name}"

        cmd = [
            self._mngr_binary,
            "create",
            name,
            "--id",
            agent_id,
            "--transfer",
            "git-worktree",
            "--branch",
            f"{current_branch}:{new_branch}",
            "--template",
            "worktree",
            "--label",
            "user_created=true",
            "--label",
            f"workspace={name}",
            "--no-connect",
        ]

        # Inherit the project label from the parent agent
        if "project" in parent_labels:
            cmd.extend(["--label", f"project={parent_labels['project']}"])

        log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)

        proto_info = {
            "agent_id": agent_id,
            "name": name,
            "creation_type": "worktree",
            "parent_agent_id": None,
        }
        with self._lock:
            self._proto_agents[agent_id] = proto_info
            self._log_queues[agent_id] = log_queue

        self._broadcaster.broadcast_proto_agent_created(
            agent_id=agent_id,
            name=name,
            creation_type="worktree",
            parent_agent_id=None,
        )

        labels = {"user_created": "true", "workspace": name}
        if "project" in parent_labels:
            labels["project"] = parent_labels["project"]
        self._launch_creation_thread(agent_id, name, cmd, Path(work_dir), log_queue, labels)

        return agent_id

    def create_chat_agent(self, name: str) -> str:
        """Create a new chat agent in the primary agent's work dir. Returns the pre-generated agent ID."""
        agent_id = str(AgentId())

        with self._lock:
            work_dir = self._resolve_agent_work_dir(self._own_agent_id)
            primary = self._agents.get(self._own_agent_id)
            primary_labels = dict(primary.labels) if primary else {}

        if work_dir is None:
            msg = f"Cannot determine work directory for primary agent {self._own_agent_id}"
            raise AgentCreationError(msg)

        cmd = [
            self._mngr_binary,
            "create",
            name,
            "--id",
            agent_id,
            "--transfer",
            "none",
            "--template",
            "chat",
            "--no-connect",
        ]

        # Inherit workspace and project labels from the primary agent
        for key in ("workspace", "project"):
            if key in primary_labels:
                cmd.extend(["--label", f"{key}={primary_labels[key]}"])

        log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)

        proto_info = {
            "agent_id": agent_id,
            "name": name,
            "creation_type": "chat",
            "parent_agent_id": None,
        }
        with self._lock:
            self._proto_agents[agent_id] = proto_info
            self._log_queues[agent_id] = log_queue

        self._broadcaster.broadcast_proto_agent_created(
            agent_id=agent_id,
            name=name,
            creation_type="chat",
            parent_agent_id=None,
        )

        labels: dict[str, str] = {}
        for key in ("workspace", "project"):
            if key in primary_labels:
                labels[key] = primary_labels[key]
        self._launch_creation_thread(agent_id, name, cmd, Path(work_dir), log_queue, labels)

        return agent_id

    def _launch_creation_thread(
        self,
        agent_id: str,
        agent_name: str,
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
        labels: dict[str, str],
    ) -> None:
        """Start a background thread to run agent creation and stream logs."""
        self._creation_cg.start_new_thread(
            target=self._run_creation,
            args=(agent_id, agent_name, cmd, work_dir, log_queue, labels),
            name=f"create-{agent_id[:8]}",
            is_checked=False,
        )

    def _resolve_agent_work_dir(self, agent_id: str) -> str | None:
        """Resolve an agent's work directory. Must be called with lock held."""
        agent = self._agents.get(agent_id)
        if agent is not None and agent.work_dir is not None:
            return agent.work_dir
        if agent_id == self._own_agent_id and self._own_work_dir:
            return self._own_work_dir
        return None

    def _get_current_branch(self, work_dir: Path) -> str:
        """Get the current git branch for a work directory."""
        result = run_local_command_modern_version(
            command=["git", "-C", str(work_dir), "branch", "--show-current"],
            cwd=None,
            is_checked=True,
        )
        return result.stdout.strip()

    def _run_creation(
        self,
        agent_id: str,
        agent_name: str,
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
        labels: dict[str, str],
    ) -> None:
        """Run mngr create in the background, capture output, and always emit completion.

        This thread is started with ``is_checked=False``, so any exception
        that escaped here was silently swallowed -- which left the client's
        ChatPanel stuck on "Creating agent..." forever, because neither the
        log stream's ``{done: true}`` sentinel nor the WS
        ``proto_agent_completed`` broadcast fired.

        The whole body runs inside a single catch-all so that *no matter
        what* the subprocess, its callbacks, or the pydantic / broadcaster
        calls below throw, the proto-agent entry is always cleared on the
        client and any error is surfaced as a string to the UI. The
        catch-all is intentional belt-and-suspenders: see
        ``test_prevent_broad_exception_catch``'s snapshot bump.
        """
        success = False
        error: str | None = None

        try:
            cmd_str = shlex.join(cmd)
            header_line = f"[cwd: {work_dir}] {cmd_str}"
            _safe_log_put(log_queue, json.dumps({"line": header_line}))

            try:
                result = run_local_command_modern_version(
                    command=cmd,
                    cwd=work_dir,
                    is_checked=False,
                    trace_output=True,
                    trace_on_line_callback=_LogQueueCallback(log_queue=log_queue),
                    shutdown_event=self._shutdown_event,
                )
                success = result.returncode == 0
                if not success:
                    error = f"mngr create exited with code {result.returncode}"
            except (OSError, ConcurrencyGroupError) as e:
                error = str(e)
                _loguru_logger.opt(exception=e).error("Error creating agent {}", agent_id)

            with self._lock:
                self._proto_agents.pop(agent_id, None)
                self._log_queues.pop(agent_id, None)
                if success:
                    self._agents[agent_id] = AgentStateItem(
                        id=agent_id,
                        name=agent_name,
                        state="RUNNING",
                        labels=labels,
                        work_dir=str(work_dir),
                    )
        except Exception as e:
            # Force-demote success: the happy path sets success=True before
            # constructing AgentStateItem, so if pydantic validation (or
            # anything else after the subprocess returned 0) raises, success
            # would still be True while _agents was never populated. That
            # would broadcast a contradictory proto_agent_completed(success=
            # True, error="Unexpected ..."). The catch-all's contract is
            # "something unexpected happened, surface it as a clean
            # failure", so force success=False regardless of prior state.
            success = False
            error = f"Unexpected {type(e).__name__}: {e}"
            _loguru_logger.opt(exception=e).error("Unexpected error creating agent {}", agent_id)
            # The proto-agent entry may still be sitting in _proto_agents if
            # the exception fired before the cleanup block. Try once more,
            # safely, before we broadcast completion.
            try:
                with self._lock:
                    self._proto_agents.pop(agent_id, None)
                    self._log_queues.pop(agent_id, None)
            except (OSError, RuntimeError) as cleanup_exc:
                _loguru_logger.opt(exception=cleanup_exc).error("Failed to clean proto-agent entry for {}", agent_id)

        _completion_signal_put(log_queue, json.dumps({"done": True, "success": success, "error": error}))
        _completion_signal_put(log_queue, None)

        if success:
            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())
        self._broadcaster.broadcast_proto_agent_completed(agent_id=agent_id, success=success, error=error)

    def _initial_discover(self) -> None:
        """Perform initial agent discovery and start application watchers."""
        try:
            agents = discover_agents()
            with self._lock:
                for agent_info in agents:
                    agent_state = AgentStateItem(
                        id=agent_info.id,
                        name=agent_info.name,
                        state=agent_info.state,
                        labels=agent_info.labels,
                        work_dir=agent_info.work_dir,
                    )
                    self._agents[agent_info.id] = agent_state

            for agent_info in agents:
                if agent_info.id == self._own_agent_id and agent_info.work_dir:
                    self._start_app_watcher(agent_info.id, Path(agent_info.work_dir))
        except (OSError, ValueError, RuntimeError, BaseMngrError) as e:
            _loguru_logger.opt(exception=e).error("Initial agent discovery failed")

    def _refresh_agents(self) -> None:
        """Re-discover all agents and broadcast updates."""
        try:
            agents = discover_agents()
            new_agents: dict[str, AgentStateItem] = {}
            for agent_info in agents:
                new_agents[agent_info.id] = AgentStateItem(
                    id=agent_info.id,
                    name=agent_info.name,
                    state=agent_info.state,
                    labels=agent_info.labels,
                    work_dir=agent_info.work_dir,
                )

            with self._lock:
                old_ids = set(self._agents.keys())
                new_ids = set(new_agents.keys())
                self._agents = new_agents

            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

            removed = old_ids - new_ids
            for agent_id in removed:
                self._stop_app_watcher(agent_id)

        except (OSError, ValueError, RuntimeError, BaseMngrError) as e:
            _loguru_logger.opt(exception=e).error("Agent refresh failed")

    def _resolve_observe_events_dir(self) -> Path:
        """Return the path to the mngr observe events directory.

        Does not create the directory; ``_start_observe`` creates it before
        spawning the subprocess.
        """
        agent_state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
        if agent_state_dir:
            return Path(agent_state_dir) / "workspace_server" / "observe"
        return Path.home() / ".mngr" / "workspace_server" / "observe"

    def _resolve_observe_cwd(self) -> Path:
        """Return the cwd for the mngr observe subprocess.

        Prefers ``MNGR_AGENT_WORK_DIR`` so observe picks up the same
        project-local ``.mngr/settings.toml`` that agent-creation commands
        run against -- the things observe lists should match what the
        primary agent could create. Falls back to ``$HOME`` when the work
        dir is unset or does not exist (e.g. tests that stub the env var
        with a non-existent path); ``$HOME`` avoids inheriting whatever
        project config happens to live under the spawning process's cwd.
        """
        work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
        if work_dir:
            candidate = Path(work_dir)
            if candidate.is_dir():
                return candidate
        return Path.home()

    def _build_observe_command(self) -> list[str]:
        """Build the argv for the mngr observe discovery-only subprocess.

        Pure: no side effects (does not create the events directory).
        """
        events_dir = self._resolve_observe_events_dir()
        return [
            self._mngr_binary,
            "observe",
            "--discovery-only",
            "--on-error",
            "continue",
            "--events-dir",
            str(events_dir),
        ]

    def _start_observe(self) -> None:
        """Start the mngr observe subprocess and a watchdog for early exit."""
        self._resolve_observe_events_dir().mkdir(parents=True, exist_ok=True)
        cmd = self._build_observe_command()

        self._observe_cg = ConcurrencyGroup(name="agent-manager-observe")
        self._observe_cg.__enter__()

        try:
            # Run from the primary agent's work dir so observe inherits the
            # same project-local .mngr/settings.toml that mngr create uses --
            # otherwise observe picks up ~/.mngr config, which inside a Docker
            # agent typically has providers enabled (e.g. modal) that are not
            # authenticated. Provider errors make `list_agents` error out,
            # which in turn prevents periodic DISCOVERY_FULL snapshots from
            # being written, so the workspace server's agent list drifts out
            # of sync with reality whenever an individual event is missed.
            process = self._observe_cg.run_process_in_background(
                command=cmd,
                cwd=self._resolve_observe_cwd(),
                on_output=self._handle_observe_output_line,
                shutdown_event=self._shutdown_event,
            )
        except (OSError, InvalidConcurrencyGroupStateError):
            _loguru_logger.warning(
                "Could not start mngr observe subprocess. Agent lifecycle events will not be detected."
            )
            self._observe_cg.__exit__(None, None, None)
            self._observe_cg = None
            return

        self._observe_process = process

        # ``run_process_in_background`` returns immediately even if the spawned
        # binary exits with a non-zero code (e.g. import failure). Attach a
        # watchdog so a silently-dying subprocess surfaces as a loud error
        # instead of a stale agent list.
        self._observe_cg.start_new_thread(
            target=self._watch_observe_process,
            args=(process,),
            name="observe-watchdog",
            is_checked=False,
        )

    def _watch_observe_process(self, process: RunningProcess) -> None:
        """Log an error if the observe subprocess exits before shutdown."""
        try:
            process.wait()
        except (ProcessError, EnvironmentStoppedError) as e:
            if self._shutdown_event.is_set():
                return
            _loguru_logger.opt(exception=e).error("mngr observe subprocess failed")
            return

        if self._shutdown_event.is_set():
            return

        stderr = process.read_stderr().strip()
        _loguru_logger.error(
            "mngr observe subprocess exited unexpectedly (returncode={}). "
            "Agent lifecycle events will no longer be detected. stderr: {}",
            process.returncode,
            stderr if stderr else "(empty)",
        )

    def _handle_observe_output_line(self, line: str, is_stdout: bool) -> None:
        """Parse and dispatch a single line of output from mngr observe.

        stderr lines are surfaced as warnings so startup failures from the
        subprocess (import errors, bad flags, etc.) are not lost.
        """
        stripped = line.strip()
        if not stripped:
            return
        if not is_stdout:
            _loguru_logger.warning("mngr observe stderr: {}", stripped)
            return
        try:
            event = parse_discovery_event_line(stripped)
            if event is not None:
                self._handle_discovery_event(event)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            _loguru_logger.opt(exception=e).error("Error parsing observe line: {}", stripped[:200])

    def _handle_discovery_event(self, event: object) -> None:
        """Handle a discovery event from mngr observe."""
        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, AgentDestroyedEvent):
            self._handle_agent_destroyed(event)
        elif isinstance(event, HostDestroyedEvent):
            self._handle_host_destroyed(event)
        else:
            pass

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Handle a full discovery snapshot."""
        new_agents: dict[str, AgentStateItem] = {}
        for agent in event.agents:
            new_agents[str(agent.agent_id)] = AgentStateItem(
                id=str(agent.agent_id),
                name=str(agent.agent_name),
                state="RUNNING",
                labels=dict(agent.labels),
                work_dir=str(agent.work_dir) if agent.work_dir else None,
            )

        with self._lock:
            old_ids = set(self._agents.keys())
            self._agents = new_agents
            new_ids = set(new_agents.keys())

        for agent_id in new_ids:
            agent = new_agents[agent_id]
            if agent_id == self._own_agent_id and agent.work_dir:
                self._start_app_watcher(agent_id, Path(agent.work_dir))

        for agent_id in old_ids - new_ids:
            self._stop_app_watcher(agent_id)

        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        """Handle an agent discovered event."""
        agent = event.agent
        agent_id = str(agent.agent_id)
        agent_state = AgentStateItem(
            id=agent_id,
            name=str(agent.agent_name),
            state="RUNNING",
            labels=dict(agent.labels),
            work_dir=str(agent.work_dir) if agent.work_dir else None,
        )

        with self._lock:
            self._agents[agent_id] = agent_state

        if agent_id == self._own_agent_id and agent_state.work_dir:
            self._start_app_watcher(agent_id, Path(agent_state.work_dir))

        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _handle_agent_destroyed(self, event: AgentDestroyedEvent) -> None:
        """Handle an agent destroyed event."""
        agent_id = str(event.agent_id)

        with self._lock:
            self._agents.pop(agent_id, None)

        self._stop_app_watcher(agent_id)
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _handle_host_destroyed(self, event: HostDestroyedEvent) -> None:
        """Handle a host destroyed event (remove all agents on that host)."""
        for agent_id in event.agent_ids:
            aid = str(agent_id)
            with self._lock:
                self._agents.pop(aid, None)
            self._stop_app_watcher(aid)

        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _start_app_watcher(self, agent_id: str, work_dir: Path) -> None:
        """Start watching runtime/applications.toml for an agent."""
        with self._lock:
            if agent_id in self._app_observers:
                return

        toml_path = work_dir / _APPLICATIONS_TOML_FILENAME
        watch_dir = toml_path.parent

        if not watch_dir.exists():
            watch_dir.mkdir(parents=True, exist_ok=True)

        self._read_applications(toml_path)

        handler = _make_applications_file_handler(agent_id, self._on_applications_changed)
        observer = _Observer()
        observer.schedule(handler, str(watch_dir), recursive=False)
        observer.daemon = True
        try:
            observer.start()
            with self._lock:
                if agent_id in self._app_observers:
                    observer.stop()
                    return
                self._app_observers[agent_id] = observer
        except OSError as e:
            _loguru_logger.opt(exception=e).error("Failed to start application watcher for agent {}", agent_id)

    def _stop_app_watcher(self, agent_id: str) -> None:
        """Stop watching applications.toml for an agent."""
        with self._lock:
            observer = self._app_observers.pop(agent_id, None)
        if observer is not None:
            observer.stop()

    def _on_applications_changed(self, agent_id: str) -> None:
        """Called when the primary agent's applications.toml changes."""
        with self._lock:
            agent = self._agents.get(agent_id)
            work_dir = agent.work_dir if agent is not None else None

        if work_dir is None:
            return

        toml_path = Path(work_dir) / _APPLICATIONS_TOML_FILENAME
        self._read_applications(toml_path)
        self._broadcaster.broadcast_applications_updated(self.get_applications_serialized())

    def _read_applications(self, toml_path: Path) -> None:
        """Read and parse runtime/applications.toml for the primary agent."""
        apps: list[ApplicationEntry] = []
        if toml_path.exists():
            try:
                data = tomllib.loads(toml_path.read_text())
                for entry in data.get("applications", []):
                    name = entry.get("name", "")
                    url = entry.get("url", "")
                    if name and url:
                        apps.append(ApplicationEntry(name=name, url=url))
            except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError) as e:
                _loguru_logger.opt(exception=e).error("Failed to parse {}", toml_path)

        with self._lock:
            self._applications = apps
