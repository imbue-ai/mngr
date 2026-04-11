import json
import os
import queue
import shlex
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import Field
from watchdog.events import DirModifiedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.event_utils import ShutdownEvent
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


class _LogQueueCallback(MutableModel):
    """Callable that appends process output lines as JSON to a queue."""

    model_config = {"arbitrary_types_allowed": True}

    log_queue: queue.Queue[str | None] = Field(description="Queue to write log lines into")

    def __call__(self, line: str, _is_stdout: bool) -> None:
        self.log_queue.put(json.dumps({"line": line.rstrip("\n")}))


class _ApplicationsFileHandler(FileSystemEventHandler):
    """Watchdog handler that triggers on modifications to applications.toml."""

    agent_id: str
    on_change: Any

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        if not event.is_directory:
            self.on_change(self.agent_id)


def _make_applications_file_handler(
    agent_id: str, on_change: Any,
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
    _applications: dict[str, list[ApplicationEntry]]
    _app_observers: dict[str, Any]
    _proto_agents: dict[str, dict[str, Any]]
    _log_queues: dict[str, queue.Queue[str | None]]
    _own_agent_id: str
    _own_work_dir: str
    _shutdown_event: ShutdownEvent
    _observe_cg: ConcurrencyGroup | None
    _creation_cg: ConcurrencyGroup

    @classmethod
    def build(cls, broadcaster: WebSocketBroadcaster) -> "AgentManager":
        """Build an AgentManager with the given broadcaster."""
        manager = cls.__new__(cls)
        manager._broadcaster = broadcaster
        manager._lock = threading.Lock()
        manager._agents = {}
        manager._applications = {}
        manager._app_observers = {}
        manager._proto_agents = {}
        manager._log_queues = {}
        manager._own_agent_id = os.environ.get("MNGR_AGENT_ID", "")
        manager._own_work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
        manager._shutdown_event = ShutdownEvent.build_root()
        manager._observe_cg = None
        manager._creation_cg = ConcurrencyGroup(name="agent-creation")
        manager._creation_cg.__enter__()
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

    def get_agents(self) -> list[AgentStateItem]:
        """Return current agent list."""
        with self._lock:
            return list(self._agents.values())

    def get_applications(self) -> dict[str, list[ApplicationEntry]]:
        """Return per-agent application map."""
        with self._lock:
            return dict(self._applications)

    def get_applications_serialized(self) -> dict[str, list[dict[str, str]]]:
        """Return per-agent application map serialized for JSON."""
        with self._lock:
            return {
                agent_id: [{"name": app.name, "url": app.url} for app in apps]
                for agent_id, apps in self._applications.items()
            }

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

        if work_dir is None:
            msg = f"Cannot determine work directory for agent {selected_agent_id}"
            raise AgentCreationError(msg)

        current_branch = self._get_current_branch(Path(work_dir))
        new_branch = f"mngr/{name}"

        cmd = [
            "mngr",
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
            "--no-connect",
        ]

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

        self._launch_creation_thread(agent_id, cmd, Path(work_dir), log_queue)

        return agent_id

    def create_chat_agent(self, name: str, parent_agent_id: str) -> str:
        """Create a new chat agent in the same work dir. Returns the pre-generated agent ID."""
        agent_id = str(AgentId())

        with self._lock:
            work_dir = self._resolve_agent_work_dir(parent_agent_id)
            parent = self._agents.get(parent_agent_id)
            parent_labels = dict(parent.labels) if parent else {}

        if work_dir is None:
            msg = f"Cannot determine work directory for agent {parent_agent_id}"
            raise AgentCreationError(msg)

        cmd = [
            "mngr",
            "create",
            name,
            "--id",
            agent_id,
            "--transfer",
            "none",
            "--template",
            "chat",
            "--label",
            f"chat_parent_id={parent_agent_id}",
            "--no-connect",
        ]

        # Inherit workspace and project labels from the parent agent
        for key in ("workspace", "project"):
            if key in parent_labels:
                cmd.extend(["--label", f"{key}={parent_labels[key]}"])

        log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)

        proto_info = {
            "agent_id": agent_id,
            "name": name,
            "creation_type": "chat",
            "parent_agent_id": parent_agent_id,
        }
        with self._lock:
            self._proto_agents[agent_id] = proto_info
            self._log_queues[agent_id] = log_queue

        self._broadcaster.broadcast_proto_agent_created(
            agent_id=agent_id,
            name=name,
            creation_type="chat",
            parent_agent_id=parent_agent_id,
        )

        self._launch_creation_thread(agent_id, cmd, Path(work_dir), log_queue)

        return agent_id

    def _launch_creation_thread(
        self,
        agent_id: str,
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
    ) -> None:
        """Start a background thread to run agent creation and stream logs."""
        self._creation_cg.start_new_thread(
            target=self._run_creation,
            args=(agent_id, cmd, work_dir, log_queue),
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
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
    ) -> None:
        """Run mngr create in the background and capture output."""
        cmd_str = shlex.join(cmd)
        header_line = f"[cwd: {work_dir}] {cmd_str}"
        log_queue.put(json.dumps({"line": header_line}))

        success = False
        error: str | None = None

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
            _loguru_logger.exception("Error creating agent {}", agent_id)

        log_queue.put(
            json.dumps({"done": True, "success": success, "error": error})
        )
        log_queue.put(None)

        with self._lock:
            self._proto_agents.pop(agent_id, None)
            self._log_queues.pop(agent_id, None)

        self._broadcaster.broadcast_proto_agent_completed(
            agent_id=agent_id, success=success, error=error
        )

        if success:
            self._refresh_agents()

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
                if agent_info.work_dir:
                    self._start_app_watcher(agent_info.id, Path(agent_info.work_dir))
        except (OSError, ValueError, RuntimeError, BaseMngrError):
            _loguru_logger.exception("Initial agent discovery failed")

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

        except (OSError, ValueError, RuntimeError, BaseMngrError):
            _loguru_logger.exception("Agent refresh failed")

    def _start_observe(self) -> None:
        """Start the mngr observe subprocess."""
        agent_state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
        if agent_state_dir:
            events_dir = Path(agent_state_dir) / "workspace_server" / "observe"
        else:
            events_dir = Path.home() / ".mngr" / "workspace_server" / "observe"

        events_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "imbue.mngr",
            "observe",
            "--discovery-only",
            "--on-error",
            "continue",
            "--events-dir",
            str(events_dir),
        ]

        self._observe_cg = ConcurrencyGroup(name="agent-manager-observe")
        self._observe_cg.__enter__()

        try:
            self._observe_cg.run_process_in_background(
                command=cmd,
                on_output=self._handle_observe_output_line,
                shutdown_event=self._shutdown_event,
            )
        except (OSError, InvalidConcurrencyGroupStateError):
            _loguru_logger.warning(
                "Could not start mngr observe subprocess. "
                "Agent lifecycle events will not be detected."
            )
            self._observe_cg.__exit__(None, None, None)
            self._observe_cg = None

    def _handle_observe_output_line(self, line: str, _is_stdout: bool) -> None:
        """Parse and dispatch a single line of output from mngr observe."""
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = parse_discovery_event_line(stripped)
            if event is not None:
                self._handle_discovery_event(event)
        except (json.JSONDecodeError, ValueError, KeyError):
            _loguru_logger.exception("Error parsing observe line: {}", stripped[:200])

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
            if agent.work_dir:
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

        if agent_state.work_dir:
            self._start_app_watcher(agent_id, Path(agent_state.work_dir))

        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _handle_agent_destroyed(self, event: AgentDestroyedEvent) -> None:
        """Handle an agent destroyed event."""
        agent_id = str(event.agent_id)

        with self._lock:
            self._agents.pop(agent_id, None)
            self._applications.pop(agent_id, None)

        self._stop_app_watcher(agent_id)
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _handle_host_destroyed(self, event: HostDestroyedEvent) -> None:
        """Handle a host destroyed event (remove all agents on that host)."""
        for agent_id in event.agent_ids:
            aid = str(agent_id)
            with self._lock:
                self._agents.pop(aid, None)
                self._applications.pop(aid, None)
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

        self._read_applications(agent_id, toml_path)

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
        except OSError:
            _loguru_logger.exception(
                "Failed to start application watcher for agent {}", agent_id
            )

    def _stop_app_watcher(self, agent_id: str) -> None:
        """Stop watching applications.toml for an agent."""
        with self._lock:
            observer = self._app_observers.pop(agent_id, None)
        if observer is not None:
            observer.stop()

    def _on_applications_changed(self, agent_id: str) -> None:
        """Called when an agent's applications.toml changes."""
        with self._lock:
            agent = self._agents.get(agent_id)
            work_dir = agent.work_dir if agent is not None else None

        if work_dir is None:
            return

        toml_path = Path(work_dir) / _APPLICATIONS_TOML_FILENAME
        self._read_applications(agent_id, toml_path)
        self._broadcaster.broadcast_applications_updated(
            self.get_applications_serialized()
        )

    def _read_applications(self, agent_id: str, toml_path: Path) -> None:
        """Read and parse runtime/applications.toml for an agent."""
        apps: list[ApplicationEntry] = []
        if toml_path.exists():
            try:
                data = tomllib.loads(toml_path.read_text())
                for entry in data.get("applications", []):
                    name = entry.get("name", "")
                    url = entry.get("url", "")
                    if name and url:
                        apps.append(ApplicationEntry(name=name, url=url))
            except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
                _loguru_logger.exception(
                    "Failed to parse {} for agent {}", toml_path, agent_id
                )

        with self._lock:
            self._applications[agent_id] = apps
