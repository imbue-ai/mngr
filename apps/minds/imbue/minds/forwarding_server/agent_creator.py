"""Agent creation for the forwarding server.

Creates mngr agents from git repositories or local directories. The repo's
own ``.mngr/settings.toml`` drives all configuration -- no minds.toml,
vendoring, or parent tracking.

Agent creation runs in background threads so the server remains responsive.
Callers can poll creation status via get_creation_info() or stream logs
via get_log_queue().
"""

import os
import queue
import shlex
import shutil
import tempfile
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import MindPaths
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.forwarding_server.cloudflare_client import CloudflareForwardingClient
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId

OutputCallback = Callable[[str, bool], None]

LOG_SENTINEL: Final[str] = "__DONE__"


def make_log_callback(log_queue: queue.Queue[str]) -> OutputCallback:
    """Create an output callback that puts lines into a queue."""
    return lambda line, is_stdout: logger.info(line.rstrip("\n")) or log_queue.put(line.rstrip("\n"))


class AgentCreationStatus(UpperCaseStrEnum):
    """Status of a background agent creation."""

    CLONING = auto()
    CREATING = auto()
    DONE = auto()
    FAILED = auto()


class AgentCreationInfo(FrozenModel):
    """Snapshot of agent creation state, returned to callers for status polling."""

    agent_id: AgentId = Field(description="ID of the agent being created")
    status: AgentCreationStatus = Field(description="Current creation status")
    redirect_url: str | None = Field(default=None, description="URL to redirect to when creation is done")
    error: str | None = Field(default=None, description="Error message, set when status is FAILED")


def extract_repo_name(git_url: str) -> str:
    """Extract a short name from a git URL or path for use as agent name.

    Strips .git suffix and trailing slashes, then takes the last path component.
    Non-alphanumeric characters (except hyphens and underscores) are replaced
    with hyphens. Falls back to 'mind' if the URL doesn't yield a usable name.
    """
    url = git_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    name = url.rsplit("/", 1)[-1]
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    cleaned = cleaned.strip("-")
    return cleaned if cleaned else "mind"


def _is_local_path(repo_source: str) -> bool:
    """Check if a repo source is a local path rather than a URL.

    Anything starting with /, ./, ../, or ~ is treated as a local path.
    Anything containing :// is treated as a URL.
    """
    if "://" in repo_source:
        return False
    return repo_source.startswith(("/", "./", "../", "~"))


def clone_git_repo(
    git_url: GitUrl,
    clone_dir: Path,
    on_output: OutputCallback | None = None,
) -> None:
    """Clone a git repository into the specified directory.

    The clone_dir must not already exist -- git clone will create it.
    Raises GitCloneError if the clone fails.
    """
    logger.debug("Cloning {} to {}", git_url, clone_dir)
    cg = ConcurrencyGroup(name="git-clone")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "clone", str(git_url), str(clone_dir)],
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise GitCloneError(
            "git clone failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


def checkout_branch(
    repo_dir: Path,
    branch: GitBranch,
    on_output: OutputCallback | None = None,
) -> None:
    """Check out a specific branch in a cloned repository.

    Raises GitOperationError if the checkout fails (e.g. branch does not exist).
    """
    logger.debug("Checking out branch {} in {}", branch, repo_dir)
    cg = ConcurrencyGroup(name="git-checkout")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "checkout", str(branch)],
            cwd=repo_dir,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise GitOperationError(
            "git checkout failed for branch '{}' (exit code {}):\n{}".format(
                branch,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


def _make_host_name(agent_name: AgentName) -> str:
    """Build the host name for a mind agent.

    Uses ``{agent_name}-host`` so it is obvious why the host was created.
    """
    return f"{agent_name}-host"


def _build_mngr_create_command(
    launch_mode: LaunchMode,
    agent_name: AgentName,
    agent_id: AgentId,
) -> list[str]:
    """Build the mngr create command for the given launch mode.

    DEV mode: --template main --template dev (runs in-place on local provider)
    LOCAL mode: --template main --template docker (runs in Docker container)
    CLOUD mode: not yet supported

    For modes that create a separate host (LOCAL, CLOUD), the agent address
    uses ``agent_name@{agent_name}-host`` so hosts are clearly attributable.
    ``--reuse`` and ``--update`` are passed so re-deploying resets the agent
    on the same host instead of failing.
    """
    match launch_mode:
        case LaunchMode.DEV:
            address = str(agent_name)
        case LaunchMode.LOCAL:
            address = f"{agent_name}@{_make_host_name(agent_name)}.docker"
        case LaunchMode.CLOUD:
            address = f"{agent_name}@{_make_host_name(agent_name)}"
        case _ as unreachable:
            assert_never(unreachable)

    mngr_command: list[str] = [
        MNGR_BINARY,
        "create",
        address,
        "--id",
        str(agent_id),
        "--no-connect",
        "--reuse",
        "--update",
        "--label",
        f"mind={agent_name}",
        "--template",
        "main",
    ]

    match launch_mode:
        case LaunchMode.DEV:
            mngr_command.extend(["--template", "dev"])
        case LaunchMode.LOCAL:
            mngr_command.extend(["--new-host", "--template", "docker"])
        case LaunchMode.CLOUD:
            raise NotImplementedError("Cloud launch mode is not yet supported")
        case _ as unreachable:
            assert_never(unreachable)

    return mngr_command


def run_mngr_create(
    launch_mode: LaunchMode,
    mind_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    on_output: OutputCallback | None = None,
) -> None:
    """Create an mngr agent via ``mngr create``.

    The repo's own ``.mngr/settings.toml`` defines agent types, templates,
    environment variables, and all other configuration.

    Raises MngrCommandError if the command fails.
    """
    mngr_command = _build_mngr_create_command(launch_mode, agent_name, agent_id)

    logger.info("Running: {}", " ".join(mngr_command))

    cg = ConcurrencyGroup(name="mngr-create")
    with cg:
        result = cg.run_process_to_completion(
            command=mngr_command,
            cwd=mind_dir,
            is_checked_after=False,
            on_output=on_output,
        )

    if result.returncode != 0:
        raise MngrCommandError(
            "mngr create failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


def _inject_tunnel_token(
    agent_id: AgentId,
    token: str,
    log_queue: queue.Queue[str],
) -> None:
    """Inject the tunnel token into the agent's runtime/secrets file via mngr exec."""
    log_queue.put("[minds] Injecting tunnel token into agent...")
    safe_token = shlex.quote(token)
    cg = ConcurrencyGroup(name="mngr-exec-token")
    with cg:
        command = [
            MNGR_BINARY,
            "exec",
            str(agent_id),
            f"mkdir -p runtime && printf 'export CLOUDFLARE_TUNNEL_TOKEN=%s\\n' {safe_token} >> runtime/secrets",
        ]
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
        )
    if result.returncode != 0:
        log_queue.put(
            "[minds] WARNING: Failed to inject tunnel token: {}".format(
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
            )
        )
    else:
        log_queue.put("[minds] Tunnel token injected successfully.")


class AgentCreator(MutableModel):
    """Creates mngr agents in the background from git repositories or local paths.

    Tracks creation status so the forwarding server can show progress
    and redirect users to agents when creation is complete.

    Thread-safe: all status reads/writes are guarded by an internal lock.
    """

    paths: MindPaths = Field(frozen=True, description="Filesystem paths for minds data")
    cloudflare_client: CloudflareForwardingClient | None = Field(
        default=None,
        frozen=True,
        description="Client for Cloudflare tunnel API, or None if not configured",
    )

    _statuses: dict[str, AgentCreationStatus] = PrivateAttr(default_factory=dict)
    _redirect_urls: dict[str, str] = PrivateAttr(default_factory=dict)
    _errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _log_queues: dict[str, queue.Queue[str]] = PrivateAttr(default_factory=dict)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_creation(
        self,
        repo_source: str,
        agent_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.LOCAL,
    ) -> AgentId:
        """Start creating an agent from a git URL or local path in a background thread.

        Returns the agent ID immediately. Use get_creation_info() to poll status,
        or iter_log_lines() to stream creation logs.
        """
        agent_id = AgentId()
        log_queue: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._statuses[str(agent_id)] = AgentCreationStatus.CLONING
            self._log_queues[str(agent_id)] = log_queue

        effective_name = agent_name.strip() if agent_name.strip() else extract_repo_name(repo_source)
        effective_branch = branch.strip()

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(agent_id, repo_source, effective_name, effective_branch, log_queue, launch_mode),
            daemon=True,
            name="agent-creator-{}".format(agent_id),
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)
        return agent_id

    def wait_for_all(self, timeout: float = 10.0) -> None:
        """Wait for all background creation threads to finish."""
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=timeout)

    def get_creation_info(self, agent_id: AgentId) -> AgentCreationInfo | None:
        """Get the current creation status for an agent, or None if not tracked."""
        with self._lock:
            status = self._statuses.get(str(agent_id))
            if status is None:
                return None
            return AgentCreationInfo(
                agent_id=agent_id,
                status=status,
                redirect_url=self._redirect_urls.get(str(agent_id)),
                error=self._errors.get(str(agent_id)),
            )

    def get_log_queue(self, agent_id: AgentId) -> queue.Queue[str] | None:
        """Get the log queue for an agent creation, or None if not tracked."""
        with self._lock:
            return self._log_queues.get(str(agent_id))

    def _create_agent_background(
        self,
        agent_id: AgentId,
        repo_source: str,
        agent_name: str,
        branch: str,
        log_queue: queue.Queue[str],
        launch_mode: LaunchMode,
    ) -> None:
        """Background thread that resolves the repo source and creates an mngr agent."""
        aid = str(agent_id)
        emit_log = make_log_callback(log_queue)
        temp_clone_dir: Path | None = None
        try:
            with log_span("Creating agent {} from {} (mode: {})", agent_id, repo_source, launch_mode):
                if _is_local_path(repo_source):
                    resolved_path = Path(os.path.expanduser(repo_source)).resolve()
                    if not resolved_path.is_dir():
                        raise MngrCommandError(f"Local path does not exist: {resolved_path}")
                    mind_dir = resolved_path
                    log_queue.put(f"[minds] Using local directory: {mind_dir}")
                else:
                    temp_clone_dir = Path(tempfile.mkdtemp(prefix="minds-clone-"))
                    clone_target = temp_clone_dir / extract_repo_name(repo_source)
                    log_queue.put("[minds] Cloning {}...".format(repo_source))
                    clone_git_repo(GitUrl(repo_source), clone_target, on_output=emit_log)
                    mind_dir = clone_target

                if branch:
                    log_queue.put("[minds] Checking out branch '{}'...".format(branch))
                    checkout_branch(mind_dir, GitBranch(branch), on_output=emit_log)

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.CREATING

                parsed_name = AgentName(agent_name)
                log_queue.put(
                    "[minds] Creating agent '{}' (mode: {})...".format(agent_name, launch_mode.value)
                )
                run_mngr_create(
                    launch_mode=launch_mode,
                    mind_dir=mind_dir,
                    agent_name=parsed_name,
                    agent_id=agent_id,
                    on_output=emit_log,
                )

                self._setup_cloudflare_tunnel(agent_id, log_queue)

                log_queue.put("[minds] Agent created successfully.")

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.DONE
                    self._redirect_urls[aid] = "/agents/{}/".format(agent_id)

        except (GitCloneError, GitOperationError, MngrCommandError, NotImplementedError, ValueError, OSError) as e:
            logger.error("Failed to create agent {}: {}", agent_id, e)
            log_queue.put("[minds] ERROR: {}".format(e))
            with self._lock:
                self._statuses[aid] = AgentCreationStatus.FAILED
                self._errors[aid] = str(e)
        finally:
            if temp_clone_dir is not None and temp_clone_dir.exists():
                shutil.rmtree(temp_clone_dir, ignore_errors=True)
            log_queue.put(LOG_SENTINEL)

    def _setup_cloudflare_tunnel(
        self,
        agent_id: AgentId,
        log_queue: queue.Queue[str],
    ) -> None:
        """Create a Cloudflare tunnel and inject its token into the agent.

        Uses the cloudflare_client if configured. Does nothing otherwise.
        """
        if self.cloudflare_client is None:
            log_queue.put("[minds] Cloudflare forwarding not configured, skipping tunnel creation.")
            return

        log_queue.put("[minds] Creating Cloudflare tunnel...")
        token, message = self.cloudflare_client.create_tunnel(agent_id)
        log_queue.put(f"[minds] {message}")

        if token is not None:
            _inject_tunnel_token(agent_id, token, log_queue)
        else:
            log_queue.put("[minds] Skipping tunnel token injection.")
