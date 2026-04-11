import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from watchdog.events import FileModifiedEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.minds_workspace_server.agent_discovery import discover_agents
from imbue.minds_workspace_server.models import AgentCreationError
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.models import ApplicationEntry
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster

_APPLICATIONS_TOML_FILENAME = "runtime/applications.toml"


class _ApplicationsFileHandler(FileSystemEventHandler):
    """Watchdog handler that triggers on modifications to applications.toml."""

    agent_id: str
    on_change: Any

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
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

    def __init__(self, broadcaster: WebSocketBroadcaster) -> None:
        self._broadcaster = broadcaster
        self._lock = threading.Lock()

        self._agents: dict[str, AgentStateItem] = {}
        self._applications: dict[str, list[ApplicationEntry]] = {}

        self._observe_process: subprocess.Popen[str] | None = None
        self._observe_thread: threading.Thread | None = None

        self._app_observers: dict[str, Any] = {}

        self._proto_agents: dict[str, dict[str, Any]] = {}
        self._log_queues: dict[str, queue.Queue[str | None]] = {}
        self._creation_threads: list[threading.Thread] = []

        self._own_agent_id = os.environ.get("MNGR_AGENT_ID", "")
        self._own_work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")

        self._shutdown = False

    def start(self) -> None:
        """Start the observe subprocess and perform initial agent discovery."""
        self._initial_discover()
        self._start_observe()

    def start_without_observe(self) -> None:
        """Start with initial discovery only, no observe subprocess. For testing."""
        self._initial_discover()

    def stop(self) -> None:
        """Stop the observe subprocess, file watchers, and creation threads."""
        self._shutdown = True

        if self._observe_process is not None:
            self._observe_process.terminate()
            try:
                self._observe_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._observe_process.kill()
            self._observe_process = None

        for observer in self._app_observers.values():
            observer.stop()
        for observer in self._app_observers.values():
            observer.join(timeout=5)
        self._app_observers.clear()

        for t in self._creation_threads:
            t.join(timeout=5)

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

        thread = threading.Thread(
            target=self._run_creation,
            args=(agent_id, cmd, Path(work_dir), log_queue),
            daemon=True,
        )
        self._creation_threads.append(thread)
        thread.start()

        return agent_id

    def create_chat_agent(self, name: str, parent_agent_id: str) -> str:
        """Create a new chat agent in the same work dir. Returns the pre-generated agent ID."""
        agent_id = str(AgentId())

        with self._lock:
            work_dir = self._resolve_agent_work_dir(parent_agent_id)

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

        thread = threading.Thread(
            target=self._run_creation,
            args=(agent_id, cmd, Path(work_dir), log_queue),
            daemon=True,
        )
        self._creation_threads.append(thread)
        thread.start()

        return agent_id

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
        result = subprocess.run(
            ["git", "-C", str(work_dir), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
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
            process = subprocess.Popen(
                cmd,
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if process.stdout is not None:
                for line in process.stdout:
                    stripped = line.rstrip("\n")
                    log_queue.put(json.dumps({"line": stripped}))

            return_code = process.wait()
            success = return_code == 0
            if not success:
                error = f"mngr create exited with code {return_code}"
        except (OSError, subprocess.SubprocessError) as e:
            error = str(e)
            _loguru_logger.exception("Error creating agent {}", agent_id)

        log_queue.put(
            json.dumps({"done": True, "success": success, "error": error})
        )
        log_queue.put(None)

        with self._lock:
            self._proto_agents.pop(agent_id, None)

        self._broadcaster.broadcast_proto_agent_completed(
            agent_id=agent_id, success=success, error=error
        )

        if success:
            self._refresh_agents()

    def _initial_discover(self) -> None:
        """Perform initial agent discovery using the existing discover_agents function."""
        try:
            agents = discover_agents()
            with self._lock:
                for agent_info in agents:
                    agent_state = AgentStateItem(
                        id=agent_info.id,
                        name=agent_info.name,
                        state=agent_info.state,
                        labels={},
                        work_dir=None,
                    )
                    self._agents[agent_info.id] = agent_state
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
                    labels={},
                    work_dir=None,
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

        try:
            self._observe_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            _loguru_logger.warning(
                "Could not start mngr observe subprocess. "
                "Agent lifecycle events will not be detected."
            )
            return

        self._observe_thread = threading.Thread(
            target=self._observe_reader,
            daemon=True,
            name="agent-manager-observe",
        )
        self._observe_thread.start()

    def _observe_reader(self) -> None:
        """Read mngr observe output line by line and handle events."""
        assert self._observe_process is not None
        assert self._observe_process.stdout is not None

        for line in self._observe_process.stdout:
            if self._shutdown:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = parse_discovery_event_line(line)
                if event is not None:
                    self._handle_discovery_event(event)
            except (json.JSONDecodeError, ValueError, KeyError):
                _loguru_logger.exception("Error parsing observe line: {}", line[:200])

        _loguru_logger.info("Observe reader thread exiting")

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
            self._app_observers[agent_id] = observer
        except OSError:
            _loguru_logger.exception(
                "Failed to start application watcher for agent {}", agent_id
            )

    def _stop_app_watcher(self, agent_id: str) -> None:
        """Stop watching applications.toml for an agent."""
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
