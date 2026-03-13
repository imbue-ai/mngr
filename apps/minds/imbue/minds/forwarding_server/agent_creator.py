"""Agent creation for the forwarding server.

Creates mng agents from git repositories. Handles cloning, agent type
resolution, and mng create invocation.

Agent creation runs in background threads so the server remains responsive.
Callers can poll creation status via get_creation_info().
"""

import shutil
import threading
import tomllib
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNG_BINARY
from imbue.minds.config.data_types import MindPaths
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import MngCommandError
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitUrl
from imbue.mng.primitives import AgentId
from imbue.mng_claude_mind.settings import SETTINGS_FILENAME
from imbue.mng_claude_mind.settings import load_settings_from_path

DEFAULT_AGENT_TYPE: Final[str] = "claude-mind"

_DEFAULT_PASS_ENV: Final[tuple[str, ...]] = ("ANTHROPIC_API_KEY",)


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


def clone_git_repo(git_url: GitUrl, clone_dir: Path) -> None:
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
        )
    if result.returncode != 0:
        raise GitCloneError(
            "git clone failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


def resolve_agent_type(repo_dir: Path) -> str:
    """Resolve agent type from minds.toml in the repo, falling back to DEFAULT_AGENT_TYPE.

    If the repo contains a minds.toml with an agent_type field, uses that value.
    Otherwise returns DEFAULT_AGENT_TYPE ('claude-mind').
    """
    settings_path = repo_dir / SETTINGS_FILENAME
    try:
        settings = load_settings_from_path(settings_path)
    except FileNotFoundError:
        return DEFAULT_AGENT_TYPE
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as e:
        logger.warning("Failed to parse {}, using default agent type: {}", settings_path, e)
        return DEFAULT_AGENT_TYPE
    if settings.agent_type is not None:
        return settings.agent_type
    return DEFAULT_AGENT_TYPE


def run_mng_create(
    mind_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    agent_type: str,
    pass_env: tuple[str, ...],
) -> None:
    """Create an mng agent via ``mng create``.

    Creates a local in-place agent with the mind=true label.
    Raises MngCommandError if the command fails.
    """
    mng_command: list[str] = [
        MNG_BINARY,
        "create",
        agent_name,
        "--id",
        str(agent_id),
        "--no-connect",
        "--type",
        agent_type,
        "--env",
        "ROLE=thinking",
        "--label",
        "mind=true",
        "--disable-plugin",
        "ttyd",
        "--yes",
        "--in-place",
    ]

    for env_var in pass_env:
        mng_command.extend(["--pass-env", env_var])

    # FOLLOWUP: remove --dangerously-skip-permissions
    mng_command.extend(["--", "--dangerously-skip-permissions"])

    logger.debug("Running: {}", " ".join(mng_command))

    cg = ConcurrencyGroup(name="mng-create")
    with cg:
        result = cg.run_process_to_completion(
            command=mng_command,
            cwd=mind_dir,
            is_checked_after=False,
        )

    if result.returncode != 0:
        raise MngCommandError(
            "mng create failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


class AgentCreator(MutableModel):
    """Creates mng agents in the background from git repositories.

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
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_creation(self, git_url: str) -> AgentId:
        """Start creating an agent from a git URL in a background thread.

        Returns the agent ID immediately. Use get_creation_info() to poll status.
        """
        agent_id = AgentId()
        with self._lock:
            self._statuses[str(agent_id)] = AgentCreationStatus.CLONING

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(agent_id, git_url),
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

    def _create_agent_background(self, agent_id: AgentId, git_url: str) -> None:
        """Background thread that clones a repo and creates an mng agent."""
        aid = str(agent_id)
        mind_dir = self.paths.mind_dir(agent_id)
        try:
            with log_span("Creating agent {} from {}", agent_id, git_url):
                self.paths.data_dir.mkdir(parents=True, exist_ok=True)

                clone_git_repo(GitUrl(git_url), mind_dir)

                agent_type = resolve_agent_type(mind_dir)
                agent_name = AgentName(extract_repo_name(git_url))

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.CREATING

                run_mng_create(
                    mind_dir=mind_dir,
                    agent_name=agent_name,
                    agent_id=agent_id,
                    agent_type=agent_type,
                    pass_env=self.pass_env,
                )

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.DONE
                    self._redirect_urls[aid] = "/agents/{}/".format(agent_id)

        except (GitCloneError, MngCommandError, ValueError, OSError) as e:
            logger.error("Failed to create agent {}: {}", agent_id, e)
            with self._lock:
                self._statuses[aid] = AgentCreationStatus.FAILED
                self._errors[aid] = str(e)
            if mind_dir.exists():
                shutil.rmtree(mind_dir, ignore_errors=True)
