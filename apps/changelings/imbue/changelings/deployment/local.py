import json
import secrets
import shutil
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.changelings.config.data_types import DeploymentProvider
from imbue.changelings.config.data_types import MNG_BINARY
from imbue.changelings.errors import AgentAlreadyExistsError
from imbue.changelings.errors import ChangelingError
from imbue.changelings.errors import GitCloneError
from imbue.changelings.errors import GitCommitError
from imbue.changelings.errors import GitInitError
from imbue.changelings.errors import MngCommandError
from imbue.changelings.forwarding_server.auth import FileAuthStore
from imbue.changelings.primitives import AgentName
from imbue.changelings.primitives import GitBranch
from imbue.changelings.primitives import GitUrl
from imbue.changelings.primitives import OneTimeCode
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mng.primitives import AgentId

_ONE_TIME_CODE_LENGTH: Final[int] = 32


class DeploymentResult(FrozenModel):
    """Result of a successful changeling deployment."""

    agent_name: AgentName = Field(description="The name of the deployed agent")
    agent_id: AgentId = Field(description="The mng agent ID (used for forwarding server routing)")
    login_url: str = Field(description="One-time login URL for accessing the changeling")


class UpdateResult(FrozenModel):
    """Result of a successful local changeling update."""

    agent_name: AgentName = Field(description="The name of the updated agent")
    did_snapshot: bool = Field(description="Whether a snapshot was created before updating")
    did_push: bool = Field(description="Whether code was pushed to the agent")
    did_provision: bool = Field(description="Whether provisioning was re-run")


class MngNotFoundError(ChangelingError):
    """Raised when the mng binary cannot be found on PATH."""

    ...


class MngCreateError(ChangelingError):
    """Raised when mng create fails."""

    ...


class AgentIdLookupError(ChangelingError):
    """Raised when the mng agent ID cannot be determined after creation."""

    ...


def clone_git_repo(git_url: GitUrl, clone_dir: Path, branch: GitBranch | None = None) -> None:
    """Clone a git repository into the specified directory.

    The clone_dir must not already exist -- git clone will create it.
    The caller is responsible for choosing a suitable location (e.g.
    under ~/.changelings/clones/).

    If branch is specified, only that branch is cloned (via git clone -b).

    Raises GitCloneError if the clone fails.
    """
    logger.debug("Cloning {} to {}", git_url, clone_dir)

    command = ["git", "clone"]
    if branch is not None:
        command.extend(["-b", str(branch)])
    command.extend([str(git_url), str(clone_dir)])

    cg = ConcurrencyGroup(name="git-clone")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
        )

    if result.returncode != 0:
        raise GitCloneError(
            "git clone failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )

    logger.debug("Cloned repository to {}", clone_dir)


def init_empty_git_repo(repo_dir: Path) -> None:
    """Initialize an empty git repository at the given path.

    Creates the directory if it does not exist. Raises GitInitError if git init fails.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Initializing empty git repo at {}", repo_dir)

    cg = ConcurrencyGroup(name="git-init")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "init"],
            cwd=repo_dir,
            is_checked_after=False,
        )

    if result.returncode != 0:
        raise GitInitError(
            "git init failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )

    logger.debug("Initialized empty git repo at {}", repo_dir)


def commit_files_in_repo(repo_dir: Path, message: str) -> bool:
    """Stage all files and commit in the given git repo.

    Uses a default author/committer identity so that commits succeed even
    in environments without a global git config (e.g. CI runners).

    Returns True if a commit was created, False if there was nothing to commit.
    Raises GitCommitError if the git operations fail unexpectedly.
    """
    cg = ConcurrencyGroup(name="git-commit")
    with cg:
        add_result = cg.run_process_to_completion(
            command=["git", "add", "."],
            cwd=repo_dir,
            is_checked_after=False,
        )

    if add_result.returncode != 0:
        raise GitCommitError(
            "git add failed (exit code {}):\n{}".format(
                add_result.returncode,
                add_result.stderr.strip() if add_result.stderr.strip() else add_result.stdout.strip(),
            )
        )

    # Check if there is anything to commit
    cg_status = ConcurrencyGroup(name="git-status")
    with cg_status:
        status_result = cg_status.run_process_to_completion(
            command=["git", "status", "--porcelain"],
            cwd=repo_dir,
            is_checked_after=False,
        )

    if not status_result.stdout.strip():
        logger.debug("No changes to commit in {}", repo_dir)
        return False

    cg_commit = ConcurrencyGroup(name="git-commit-run")
    with cg_commit:
        commit_result = cg_commit.run_process_to_completion(
            command=[
                "git",
                "-c",
                "user.name=changeling",
                "-c",
                "user.email=changeling@localhost",
                "commit",
                "-m",
                message,
            ],
            cwd=repo_dir,
            is_checked_after=False,
        )

    if commit_result.returncode != 0:
        raise GitCommitError(
            "git commit failed (exit code {}):\n{}".format(
                commit_result.returncode,
                commit_result.stderr.strip() if commit_result.stderr.strip() else commit_result.stdout.strip(),
            )
        )

    logger.debug("Committed files in {}: {}", repo_dir, message)
    return True


def deploy_changeling(
    changeling_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    provider: DeploymentProvider,
    paths: ChangelingPaths,
    forwarding_server_port: int,
    concurrency_group: ConcurrencyGroup,
) -> DeploymentResult:
    """Deploy a changeling by creating an mng agent.

    The changeling_dir is the permanent changeling directory (e.g.
    ~/.changelings/<agent-id>/) containing the prepared repo. The caller
    generates the agent_id upfront and uses it for both the directory name
    and the --agent-id flag passed to mng create, ensuring they match.

    For local deployments, the agent is created with --in-place so it
    runs directly in changeling_dir.

    For remote deployments (Modal, Docker), the code is copied from
    changeling_dir to the remote host via --source-path. The caller
    is responsible for cleaning up changeling_dir afterwards.

    This function:
    1. Verifies mng is available and no agent with this name exists
    2. Creates an mng agent via `mng create --agent-id <id> -t entrypoint --label changeling=true`
    3. Generates a one-time auth code for the forwarding server
    4. Returns the deployment result with the login URL

    The agent itself is responsible for writing its server info to
    $MNG_AGENT_STATE_DIR/logs/servers.jsonl on startup, which the forwarding
    server reads to discover backends.
    """
    with log_span("Deploying changeling '{}' via provider '{}'", agent_name, provider.value):
        _verify_mng_available()

        _check_agent_not_exists(
            agent_name=agent_name,
            concurrency_group=concurrency_group,
        )

        _create_mng_agent(
            changeling_dir=changeling_dir,
            agent_name=agent_name,
            agent_id=agent_id,
            provider=provider,
            concurrency_group=concurrency_group,
        )

        login_url = _generate_auth_code(
            paths=paths,
            agent_id=agent_id,
            forwarding_server_port=forwarding_server_port,
        )

        return DeploymentResult(
            agent_name=agent_name,
            agent_id=agent_id,
            login_url=login_url,
        )


def _verify_mng_available() -> None:
    """Verify that the mng binary is available on PATH."""
    if shutil.which(MNG_BINARY) is None:
        raise MngNotFoundError("The 'mng' command was not found on PATH. Install mng first: uv tool install mng")


def _check_agent_not_exists(
    agent_name: AgentName,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Check that no agent with this name already exists.

    Raises AgentAlreadyExistsError if an agent with the given name is found.
    """
    result = concurrency_group.run_process_to_completion(
        command=[
            MNG_BINARY,
            "list",
            "--include",
            'name == "{}"'.format(agent_name),
            "--json",
        ],
        is_checked_after=False,
    )

    if result.returncode != 0:
        logger.warning("Agent existence check failed (exit code {}), proceeding without check", result.returncode)
        return

    _raise_if_agent_exists(agent_name, result.stdout)


def _raise_if_agent_exists(agent_name: AgentName, mng_list_output: str) -> None:
    """Parse mng list JSON output and raise if an agent with the given name exists.

    Silently returns if the output cannot be parsed as JSON (defensive -- the caller
    already verified the subprocess succeeded).
    """
    try:
        data = json.loads(mng_list_output)
    except json.JSONDecodeError:
        logger.warning("Failed to parse mng list output for existence check, proceeding without check")
        return

    agents = data.get("agents", [])
    if agents:
        raise AgentAlreadyExistsError(
            "An agent named '{}' already exists. "
            "Use 'changeling update' to update it, or 'changeling destroy' to remove it.".format(agent_name)
        )


def _create_mng_agent(
    changeling_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    provider: DeploymentProvider,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Create an mng agent from the changeling's repo directory.

    The agent_id is passed via --agent-id so mng uses the same ID as the
    changeling directory name (~/.changelings/<agent-id>/).

    For local deployment, runs `mng create --in-place` so the agent runs
    directly in changeling_dir.

    For remote deployment, runs `mng create --in <provider> --source-path
    <changeling_dir>`, which copies the code to the remote host.

    Both paths add `--label changeling=true` and `-t entrypoint`.
    """
    with log_span("Creating mng agent '{}' via provider '{}'", agent_name, provider.value):
        mng_command = [
            MNG_BINARY,
            "create",
            "--name",
            agent_name,
            "--agent-id",
            str(agent_id),
            "--no-connect",
            "-t",
            "entrypoint",
            "--label",
            "changeling=true",
        ]

        if provider == DeploymentProvider.LOCAL:
            # Local: run in-place so the agent runs directly in the
            # permanent changeling directory.
            mng_command.append("--in-place")
        else:
            # Remote: use the changeling directory as source and deploy
            # to the remote provider. The caller cleans up the directory.
            mng_command.extend(
                [
                    "--in",
                    provider.value.lower(),
                    "--source-path",
                    str(changeling_dir),
                ]
            )

        logger.debug("Running: {}", " ".join(mng_command))

        result = concurrency_group.run_process_to_completion(
            command=mng_command,
            cwd=changeling_dir,
            is_checked_after=False,
        )

        if result.returncode != 0:
            raise MngCreateError(
                "mng create failed (exit code {}):\n{}".format(
                    result.returncode,
                    result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                )
            )

        logger.debug("mng create output: {}", result.stdout.strip())


def _get_agent_id(
    agent_name: AgentName,
    concurrency_group: ConcurrencyGroup,
) -> AgentId:
    """Look up the mng agent ID by name using `mng list --json`."""
    with log_span("Looking up agent ID for '{}'", agent_name):
        result = concurrency_group.run_process_to_completion(
            command=[
                MNG_BINARY,
                "list",
                "--include",
                'name == "{}"'.format(agent_name),
                "--json",
            ],
            is_checked_after=False,
        )

        if result.returncode != 0:
            raise AgentIdLookupError(
                "Failed to look up agent ID for '{}': {}".format(
                    agent_name,
                    result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                )
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise AgentIdLookupError("Failed to parse mng list output: {}".format(e)) from e

        agents = data.get("agents", [])
        if not agents:
            raise AgentIdLookupError("No agent found with name '{}'".format(agent_name))

        return AgentId(agents[0]["id"])


def _generate_auth_code(
    paths: ChangelingPaths,
    agent_id: AgentId,
    forwarding_server_port: int,
) -> str:
    """Generate a one-time auth code and return the login URL."""
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    code = OneTimeCode(secrets.token_urlsafe(_ONE_TIME_CODE_LENGTH))
    auth_store.add_one_time_code(agent_id=agent_id, code=code)

    return "http://127.0.0.1:{}/login?agent_id={}&one_time_code={}".format(
        forwarding_server_port,
        agent_id,
        code,
    )


def update_local(
    agent_name: AgentName,
    do_snapshot: bool,
    do_push: bool,
    do_provision: bool,
    concurrency_group: ConcurrencyGroup,
) -> UpdateResult:
    """Update an existing locally-deployed changeling.

    Performs the following steps in order:
    1. Snapshot the agent's host (if do_snapshot is True)
    2. Stop the agent
    3. Push new code/content (if do_push is True)
    4. Re-run provisioning (if do_provision is True)
    5. Start the agent

    Raises MngCommandError if any mng command fails.
    Raises AgentIdLookupError if the agent cannot be found.
    """
    with log_span("Updating changeling '{}' locally", agent_name):
        _verify_mng_available()

        # Verify agent exists before doing anything
        _get_agent_id(
            agent_name=agent_name,
            concurrency_group=concurrency_group,
        )

        did_snapshot = False
        if do_snapshot:
            _run_mng_snapshot(agent_name=agent_name, concurrency_group=concurrency_group)
            did_snapshot = True

        _run_mng_stop(agent_name=agent_name, concurrency_group=concurrency_group)

        did_push = False
        if do_push:
            _run_mng_push(agent_name=agent_name, concurrency_group=concurrency_group)
            did_push = True

        did_provision = False
        if do_provision:
            _run_mng_provision(agent_name=agent_name, concurrency_group=concurrency_group)
            did_provision = True

        _run_mng_start(agent_name=agent_name, concurrency_group=concurrency_group)

        return UpdateResult(
            agent_name=agent_name,
            did_snapshot=did_snapshot,
            did_push=did_push,
            did_provision=did_provision,
        )


def _run_mng_command(
    command_name: str,
    args: list[str],
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Run an mng CLI command and raise MngCommandError on failure."""
    full_command = [MNG_BINARY] + args

    with log_span("Running mng {}", command_name):
        logger.debug("Running: {}", " ".join(full_command))

        result = concurrency_group.run_process_to_completion(
            command=full_command,
            is_checked_after=False,
        )

        if result.returncode != 0:
            raise MngCommandError(
                "mng {} failed (exit code {}):\n{}".format(
                    command_name,
                    result.returncode,
                    result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                )
            )

        logger.debug("mng {} output: {}", command_name, result.stdout.strip())


def _run_mng_snapshot(agent_name: AgentName, concurrency_group: ConcurrencyGroup) -> None:
    """Create a snapshot of the agent's host via `mng snapshot`."""
    logger.info("Creating snapshot of '{}'...", agent_name)
    _run_mng_command(
        command_name="snapshot",
        args=["snapshot", agent_name],
        concurrency_group=concurrency_group,
    )
    logger.info("Snapshot created.")


def _run_mng_stop(agent_name: AgentName, concurrency_group: ConcurrencyGroup) -> None:
    """Stop the agent via `mng stop`."""
    logger.info("Stopping '{}'...", agent_name)
    _run_mng_command(
        command_name="stop",
        args=["stop", agent_name],
        concurrency_group=concurrency_group,
    )
    logger.info("Agent stopped.")


def _run_mng_push(agent_name: AgentName, concurrency_group: ConcurrencyGroup) -> None:
    """Push code/content to the agent via `mng push`."""
    logger.info("Pushing to '{}'...", agent_name)
    _run_mng_command(
        command_name="push",
        args=["push", agent_name],
        concurrency_group=concurrency_group,
    )
    logger.info("Push complete.")


def _run_mng_provision(agent_name: AgentName, concurrency_group: ConcurrencyGroup) -> None:
    """Re-run provisioning on the agent via `mng provision`."""
    logger.info("Provisioning '{}'...", agent_name)
    _run_mng_command(
        command_name="provision",
        args=["provision", agent_name, "--no-restart"],
        concurrency_group=concurrency_group,
    )
    logger.info("Provisioning complete.")


def _run_mng_start(agent_name: AgentName, concurrency_group: ConcurrencyGroup) -> None:
    """Start the agent via `mng start`."""
    logger.info("Starting '{}'...", agent_name)
    _run_mng_command(
        command_name="start",
        args=["start", agent_name],
        concurrency_group=concurrency_group,
    )
    logger.info("Agent started.")
