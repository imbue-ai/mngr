"""Agent creation for the forwarding server.

Creates mngr agents from git repositories. Handles cloning, agent type
resolution, and mngr create invocation.

Agent creation runs in background threads so the server remains responsive.
Callers can poll creation status via get_creation_info() or stream logs
via get_log_queue().
"""

import os.path
import queue
import shutil
import threading
import tomllib
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

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
from imbue.minds.forwarding_server.mngr_settings import configure_mngr_settings
from imbue.minds.forwarding_server.parent_tracking import setup_mind_branch_and_parent
from imbue.minds.forwarding_server.vendor_mngr import apply_vendor_overrides
from imbue.minds.forwarding_server.vendor_mngr import default_vendor_configs
from imbue.minds.forwarding_server.vendor_mngr import vendor_repos
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId
from imbue.mngr_claude_mind.data_types import ClaudeMindSettings
from imbue.mngr_llm.settings import SETTINGS_FILENAME
from imbue.mngr_llm.settings import load_from_path

OutputCallback = Callable[[str, bool], None]

DEFAULT_AGENT_TYPE: Final[str] = "claude-mind"

_DEFAULT_PASS_ENV: Final[tuple[str, ...]] = ("ANTHROPIC_API_KEY", "GH_TOKEN")

LOG_SENTINEL: Final[str] = "__DONE__"


def make_log_callback(log_queue: queue.Queue[str]) -> OutputCallback:
    """Create an output callback that puts lines into a queue."""
    return lambda line, is_stdout: log_queue.put(line.rstrip("\n"))


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
    """Extract a short name from a git URL for use as agent name.

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


def load_creation_settings(repo_dir: Path) -> ClaudeMindSettings:
    """Load ClaudeMindSettings from minds.toml in the repo, falling back to defaults.

    Returns the parsed settings (with defaults for any missing values).
    Used during agent creation to read both agent_type and vendor config.
    """
    settings_path = repo_dir / SETTINGS_FILENAME
    try:
        return load_from_path(settings_path, ClaudeMindSettings)
    except FileNotFoundError:
        return ClaudeMindSettings()
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as e:
        logger.warning("Failed to parse {}, using defaults: {}", settings_path, e)
        return ClaudeMindSettings()


def run_mngr_create(
    launch_mode: LaunchMode,
    mind_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    agent_type: str,
    pass_env: tuple[str, ...],
    on_output: OutputCallback | None = None,
) -> None:
    """Create an mngr agent via ``mngr create``.

    Builds the appropriate command based on launch_mode:
    - DEV: runs in-place on the local provider.
    - LOCAL: runs in a Docker container.
    - CLOUD: raises NotImplementedError.

    Raises MngrCommandError if the command fails.
    """
    match launch_mode:
        case LaunchMode.CLOUD:
            raise NotImplementedError("Cloud launch mode is not yet supported")
        case LaunchMode.DEV | LaunchMode.LOCAL:
            pass
        case _ as unreachable:
            assert_never(unreachable)

    mngr_command: list[str] = [
        MNGR_BINARY,
        "create",
        agent_name,
        "--id",
        str(agent_id),
        "--no-connect",
        "--type",
        agent_type,
        "--host-env",
        "IS_AUTONOMOUS=1",
        "--env",
        "ROLE=thinking",
        "--env",
        f"MIND_NAME={agent_name}",
        "--label",
        f"mind={agent_name}",
        "--disable-plugin",
        "ttyd",
        "--yes",
    ]

    match launch_mode:
        case LaunchMode.DEV:
            mngr_command.append("--transfer=none")
        case LaunchMode.LOCAL:
            # stick the source into some canonical location
            mngr_command.extend([
                "--target-path",
                "/code/",
            ])
            remote_data_dir = os.path.expanduser(f"~/.minds/data/{agent_id}")
            Path(remote_data_dir).mkdir(parents=True, exist_ok=True)
            mngr_command.extend(
                [
                    "--provider",
                    "docker",
                    "--host-env",
                    "IS_SANDBOX=1",
                    "-vv",
                    "--source-path",
                    str(mind_dir),
                    "-s",
                    "-v={}:{}".format(remote_data_dir, "/data/remote"),
                ]
            )
            # If the source directory contains a Dockerfile, use it for the build
            dockerfile_path = mind_dir / "Dockerfile"
            if dockerfile_path.is_file():
                mngr_command.extend(["-b", "--file={}".format(dockerfile_path), "-b", str(mind_dir)])
            else:
                raise Exception("Hmmm, idk about that")
        case _ as unreachable:
            assert_never(unreachable)

    for env_var in pass_env:
        mngr_command.extend(["--pass-env", env_var])

    # FOLLOWUP: remove --dangerously-skip-permissions
    mngr_command.extend(["--", "--dangerously-skip-permissions"])

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


class AgentCreator(MutableModel):
    """Creates mngr agents in the background from git repositories.

    Tracks creation status so the forwarding server can show progress
    and redirect users to agents when creation is complete.

    Thread-safe: all status reads/writes are guarded by an internal lock.
    """

    paths: MindPaths = Field(frozen=True, description="Filesystem paths for minds data")
    pass_env: tuple[str, ...] = Field(
        default=_DEFAULT_PASS_ENV,
        frozen=True,
        description="Environment variables to forward to the agent",
    )

    _statuses: dict[str, AgentCreationStatus] = PrivateAttr(default_factory=dict)
    _redirect_urls: dict[str, str] = PrivateAttr(default_factory=dict)
    _errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _log_queues: dict[str, queue.Queue[str]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_creation(
        self,
        git_url: str,
        agent_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.LOCAL,
    ) -> AgentId:
        """Start creating an agent from a git URL in a background thread.

        Returns the agent ID immediately. Use get_creation_info() to poll status,
        or iter_log_lines() to stream creation logs.
        """
        agent_id = AgentId()
        log_queue: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._statuses[str(agent_id)] = AgentCreationStatus.CLONING
            self._log_queues[str(agent_id)] = log_queue

        effective_name = agent_name.strip() if agent_name.strip() else extract_repo_name(git_url)
        effective_branch = branch.strip()

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(agent_id, git_url, effective_name, effective_branch, log_queue, launch_mode),
            daemon=True,
            name="agent-creator-{}".format(agent_id),
        )
        thread.start()
        return agent_id

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
        git_url: str,
        agent_name: str,
        branch: str,
        log_queue: queue.Queue[str],
        launch_mode: LaunchMode,
    ) -> None:
        """Background thread that clones a repo and creates an mngr agent."""
        aid = str(agent_id)
        mind_dir = self.paths.mind_dir(agent_id)
        emit_log = make_log_callback(log_queue)
        try:
            with log_span("Creating agent {} from {} (mode: {})", agent_id, git_url, launch_mode):
                self.paths.data_dir.mkdir(parents=True, exist_ok=True)

                log_queue.put("[minds] Cloning {}...".format(git_url))
                clone_git_repo(GitUrl(git_url), mind_dir, on_output=emit_log)

                # Check out the specified branch before setting up parent tracking
                if branch:
                    log_queue.put("[minds] Checking out branch '{}'...".format(branch))
                    checkout_branch(mind_dir, GitBranch(branch), on_output=emit_log)

                log_queue.put("[minds] Setting up branch and parent tracking...")
                setup_mind_branch_and_parent(mind_dir, AgentName(agent_name), GitUrl(git_url), on_output=emit_log)

                log_queue.put("[minds] Configuring mngr settings...")
                configure_mngr_settings(mind_dir, AgentName(agent_name), agent_id, on_output=emit_log)

                settings = load_creation_settings(mind_dir)

                vendor_configs = apply_vendor_overrides(
                    settings.vendor if settings.vendor else default_vendor_configs()
                )

                log_queue.put("[minds] Vendoring {} repo(s)...".format(len(vendor_configs)))
                vendor_repos(mind_dir, vendor_configs, on_output=emit_log)

                agent_type = settings.agent_type if settings.agent_type is not None else DEFAULT_AGENT_TYPE
                parsed_name = AgentName(agent_name)

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.CREATING

                self._run_create_for_mode(
                    launch_mode=launch_mode,
                    mind_dir=mind_dir,
                    agent_name=parsed_name,
                    agent_id=agent_id,
                    agent_type=agent_type,
                    log_queue=log_queue,
                    emit_log=emit_log,
                )

                log_queue.put("[minds] Agent created successfully.")

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.DONE
                    self._redirect_urls[aid] = "/agents/{}/".format(agent_id)

        except (GitCloneError, MngrCommandError, GitOperationError, NotImplementedError, ValueError, OSError) as e:
            logger.error("Failed to create agent {}: {}", agent_id, e)
            log_queue.put("[minds] ERROR: {}".format(e))
            with self._lock:
                self._statuses[aid] = AgentCreationStatus.FAILED
                self._errors[aid] = str(e)
            if mind_dir.exists():
                shutil.rmtree(mind_dir, ignore_errors=True)
        finally:
            log_queue.put(LOG_SENTINEL)

    def _run_create_for_mode(
        self,
        launch_mode: LaunchMode,
        mind_dir: Path,
        agent_name: AgentName,
        agent_id: AgentId,
        agent_type: str,
        log_queue: queue.Queue[str],
        emit_log: OutputCallback,
    ) -> None:
        """Run mngr create for the given launch mode, then perform any post-creation cleanup."""
        log_queue.put(
            "[minds] Creating agent '{}' (type: {}, mode: {})...".format(agent_name, agent_type, launch_mode.value)
        )
        run_mngr_create(
            launch_mode=launch_mode,
            mind_dir=mind_dir,
            agent_name=agent_name,
            agent_id=agent_id,
            agent_type=agent_type,
            pass_env=self.pass_env,
            on_output=emit_log,
        )
        match launch_mode:
            case LaunchMode.LOCAL:
                # The real data lives inside the Docker container, so clean up the
                # local clone directory immediately.
                log_queue.put("[minds] Cleaning up local clone directory...")
                shutil.rmtree(mind_dir, ignore_errors=True)
            case LaunchMode.DEV | LaunchMode.CLOUD:
                pass
            case _ as unreachable:
                assert_never(unreachable)
