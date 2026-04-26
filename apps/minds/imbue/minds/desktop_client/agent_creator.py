"""Agent creation for the desktop client.

Creates mngr agents from git repositories or local directories. The repo's
own ``.mngr/settings.toml`` drives all configuration -- no minds.toml,
vendoring, or parent tracking.

Agent creation runs in background threads so the server remains responsive.
Callers can poll creation status via get_creation_info() or stream logs
via get_log_queue().
"""

import json
import os
import queue
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never
from uuid import UUID

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.host_pool_client import HostPoolError
from imbue.minds.desktop_client.host_pool_client import LeaseHostResult
from imbue.minds.desktop_client.latchkey.gateway import AGENT_SIDE_LATCHKEY_PORT
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayError
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayManager
from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyClient
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyError
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId


def _make_child_cg(name: str, parent: ConcurrencyGroup | None) -> ConcurrencyGroup:
    """Create a ``ConcurrencyGroup`` named ``name`` that is a child of ``parent``.

    ``AgentCreator`` always supplies its ``root_concurrency_group`` (required
    field), so the ``parent is None`` branch only fires when a module-level
    helper (``clone_git_repo``, ``checkout_branch``, ``resolve_template_version``)
    is called standalone by a test that doesn't thread a root CG in. Those
    helpers still accept ``parent_cg=None`` for test ergonomics.
    """
    if parent is None:
        return ConcurrencyGroup(name=name)
    return parent.make_concurrency_group(name=name)


def _leased_agent_address(agent_id: AgentId) -> str:
    """Explicit mngr address for a leased agent.

    Uses ``<agent-id>@leased-<agent-id>.ssh`` so mngr discovery only loads the
    SSH provider configured by ``imbue.minds.bootstrap._ensure_mngr_settings``
    and skips name/ID lookup entirely. This assumes the leased-mode invariant
    that the canonical agent id equals ``lease_result.agent_id`` (set in
    ``_lease_host_synchronously``).
    """
    return f"{agent_id}@leased-{agent_id}.ssh"


OutputCallback = Callable[[str, bool], None]

LOG_SENTINEL: Final[str] = "__DONE__"

# Placeholder ANTHROPIC_API_KEY set on pre-created pool hosts. Must look like
# a real Anthropic key (correct prefix, correct length ~108 chars) so that
# Claude Code validation accepts it during provisioning. Replaced with the
# real LiteLLM virtual key via sed during lease setup.
PLACEHOLDER_ANTHROPIC_API_KEY: Final[str] = (
    "sk-ant-api03-PLACEHOLDER000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
)


@pure
def _build_inject_anthropic_command(
    litellm_key: str,
    litellm_base_url: str,
    env_path: str,
) -> str:
    """Build a shell command that sets ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL in the agent env file."""
    return (
        "sed -i '/^ANTHROPIC_API_KEY=/d' {path}"
        " && echo 'ANTHROPIC_API_KEY={real_key}' >> {path}"
        " && sed -i '/^ANTHROPIC_BASE_URL=/d' {path}"
        " && echo 'ANTHROPIC_BASE_URL={base_url}' >> {path}"
    ).format(
        real_key=litellm_key,
        path=env_path,
        base_url=litellm_base_url,
    )


@pure
def _build_patch_claude_config_command(
    litellm_key: str,
    agent_id: AgentId,
) -> str:
    """Build a shell command that patches the agent's claude config to approve the LiteLLM key."""
    claude_config_path = "/mngr/agents/{}/plugin/claude/anthropic/.claude.json".format(agent_id)
    key_suffix = litellm_key[-20:]
    return (
        'python3 -c "'
        "import json; "
        "p='{path}'; "
        "d=json.load(open(p)); "
        "d['primaryApiKey']='{key}'; "
        "a=d.setdefault('customApiKeyResponses',{{}}).setdefault('approved',[]); "
        "s='{suffix}'; "
        "a.append(s) if s not in a else None; "
        "d['customApiKeyResponses']['rejected']=[]; "
        "json.dump(d,open(p,'w'),indent=2)"
        '"'
    ).format(
        path=claude_config_path,
        key=litellm_key,
        suffix=key_suffix,
    )


def make_log_callback(log_queue: queue.Queue[str]) -> OutputCallback:
    """Create an output callback that puts lines into a queue."""
    return lambda line, is_stdout: logger.info(line.rstrip("\n")) or log_queue.put(line.rstrip("\n"))


class AgentCreationStatus(UpperCaseStrEnum):
    """Status of a background agent creation."""

    CLONING = auto()
    CREATING = auto()
    DONE = auto()
    FAILED = auto()


class AgentDestructionStatus(UpperCaseStrEnum):
    """Status of a background agent destruction."""

    DESTROYING = auto()
    DONE = auto()
    FAILED = auto()


class AgentCreationInfo(FrozenModel):
    """Snapshot of agent creation state, returned to callers for status polling."""

    agent_id: AgentId = Field(description="ID of the agent being created")
    status: AgentCreationStatus = Field(description="Current creation status")
    redirect_url: str | None = Field(default=None, description="URL to redirect to when creation is done")
    error: str | None = Field(default=None, description="Error message, set when status is FAILED")


class AgentDestructionInfo(FrozenModel):
    """Snapshot of agent destruction state."""

    agent_id: AgentId = Field(description="ID of the agent being destroyed")
    status: AgentDestructionStatus = Field(description="Current destruction status")
    error: str | None = Field(default=None, description="Error message, set when status is FAILED")


def extract_repo_name(git_url: str) -> str:
    """Extract a short name from a git URL or path for use as agent name.

    Strips .git suffix and trailing slashes, then takes the last path component.
    Non-alphanumeric characters (except hyphens and underscores) are replaced
    with hyphens. Falls back to 'workspace' if the URL doesn't yield a usable name.
    """
    url = git_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    name = url.rsplit("/", 1)[-1]
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    cleaned = cleaned.strip("-")
    return cleaned if cleaned else "workspace"


def _is_local_path(repo_source: str) -> bool:
    """Check if a repo source is a local path rather than a URL.

    Anything starting with /, ./, ../, or ~ is treated as a local path.
    Anything containing :// is treated as a URL.
    """
    if "://" in repo_source:
        return False
    return repo_source.startswith(("/", "./", "../", "~"))


def _is_git_worktree(repo_dir: Path) -> bool:
    """Check if a directory is a git worktree (not the main repo).

    In a worktree, ``.git`` is a file containing ``gitdir: <path>`` rather
    than a directory. Docker copies this file as-is, but the target path
    doesn't exist inside the container, breaking git operations.
    """
    dot_git = repo_dir / ".git"
    return dot_git.is_file()


def clone_git_repo(
    git_url: GitUrl,
    clone_dir: Path,
    on_output: OutputCallback | None = None,
    *,
    is_shallow: bool = False,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Clone a git repository into the specified directory.

    The clone_dir must not already exist -- git clone will create it.
    When is_shallow is True, clones with --depth 1 to skip history.
    Raises GitCloneError if the clone fails.
    """
    logger.debug("Cloning {} to {}", git_url, clone_dir)
    command = ["git", "clone"]
    if is_shallow:
        command.extend(["--depth", "1"])
    command.extend([str(git_url), str(clone_dir)])
    cg = _make_child_cg("git-clone", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
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
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Check out a specific branch in a cloned repository.

    Raises GitOperationError if the checkout fails (e.g. branch does not exist).
    """
    logger.debug("Checking out branch {} in {}", branch, repo_dir)
    cg = _make_child_cg("git-checkout", parent_cg)
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


def _rsync_worktree_over_clone(
    worktree_dir: Path,
    clone_dir: Path,
    on_output: OutputCallback | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Rsync a worktree's working directory over a shallow clone.

    Copies all files from the worktree into the clone, preserving the
    clone's ``.git`` directory (which is a proper standalone git dir,
    unlike the worktree's ``.git`` file). This ensures uncommitted
    changes in the worktree are present in the clone.
    """
    logger.debug("Rsyncing worktree {} over clone {}", worktree_dir, clone_dir)
    command = [
        "rsync",
        "-a",
        "--delete",
        "--exclude=.git",
        "--exclude=__pycache__",
        "--exclude=.venv",
        "--exclude=node_modules",
        "--exclude=.mypy_cache",
        "--exclude=.ruff_cache",
        "--exclude=.pytest_cache",
        "--exclude=.test_output",
        f"{worktree_dir}/",
        f"{clone_dir}/",
    ]
    cg = _make_child_cg("rsync-worktree", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        logger.warning(
            "rsync worktree over clone exited with code {}: {}",
            result.returncode,
            result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
        )


def _make_host_name(agent_name: AgentName) -> str:
    """Build the host name for an agent.

    Uses ``{agent_name}-host`` so it is obvious why the host was created.
    """
    return f"{agent_name}-host"


WELCOME_INITIAL_MESSAGE: Final[str] = "/welcome"


def _build_latchkey_gateway_url(launch_mode: LaunchMode, info: LatchkeyGatewayInfo) -> str:
    """Return the ``LATCHKEY_GATEWAY`` URL the agent should see in its environment.

    DEV agents run on the bare host and reach the gateway on its dynamic host
    port directly. Every other mode (container / VM / VPS / leased pool host)
    runs inside an isolated runtime whose own loopback is bridged to the
    host-side gateway via an SSH reverse tunnel bound to a fixed remote port,
    so the URL is the same constant for every such agent.
    """
    match launch_mode:
        case LaunchMode.DEV:
            return f"http://{info.host}:{info.port}"
        case LaunchMode.LOCAL | LaunchMode.LIMA | LaunchMode.CLOUD | LaunchMode.LEASED:
            return f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
        case _ as unreachable:
            assert_never(unreachable)


def _build_mngr_create_command(
    launch_mode: LaunchMode,
    agent_name: AgentName,
    agent_id: AgentId,
    host_env_file: Path | None = None,
    latchkey_gateway_url: str | None = None,
) -> tuple[list[str], str]:
    """Build the mngr create command and generate an API key for the agent.

    Returns (command_list, api_key) where api_key is a UUID4 string injected
    as MINDS_API_KEY into the agent's environment via --env.

    DEV mode: --template main --template dev (runs in-place on local provider)
    LOCAL mode: --template main --template docker (runs in Docker container)
    LIMA mode: --template main --template lima (runs in Lima VM)
    CLOUD mode: --template main --template vultr (runs in Docker on a Vultr VPS)

    For modes that create a separate host (LOCAL, LIMA, CLOUD), the agent address
    uses ``agent_name@{agent_name}-host`` so hosts are clearly attributable.
    ``--reuse`` and ``--update`` are passed so re-deploying resets the agent
    on the same host instead of failing.

    When ``host_env_file`` is supplied, its contents are loaded into the host
    environment via ``--host-env-file`` so secrets from a local ``.env`` reach
    the agent without being baked into the template.

    When ``latchkey_gateway_url`` is supplied, it is injected as
    ``LATCHKEY_GATEWAY=<url>`` so the agent's ``latchkey`` CLI forwards
    its calls to the gateway minds is running for this agent.
    """
    match launch_mode:
        case LaunchMode.DEV:
            address = str(agent_name)
        case LaunchMode.LOCAL:
            address = f"{agent_name}@{_make_host_name(agent_name)}.docker"
        case LaunchMode.LIMA:
            address = f"{agent_name}@{_make_host_name(agent_name)}.lima"
        case LaunchMode.CLOUD:
            address = f"{agent_name}@{_make_host_name(agent_name)}.vultr"
        case LaunchMode.LEASED:
            raise MngrCommandError("LEASED mode does not use mngr create -- use the host pool lease flow instead")
        case _ as unreachable:
            assert_never(unreachable)

    api_key = generate_api_key()

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
        f"workspace={agent_name}",
        "--env",
        f"MINDS_API_KEY={api_key}",
        "--label",
        "user_created=true",
        *(["--env", f"LATCHKEY_GATEWAY={latchkey_gateway_url}"] if latchkey_gateway_url else []),
        "--label",
        "is_primary=true",
        "--template",
        "main",
        "--message",
        WELCOME_INITIAL_MESSAGE,
    ]

    match launch_mode:
        case LaunchMode.DEV:
            # Local (same-machine) mode: the agent inherits the bootstrap-set
            # MNGR_HOST_DIR/MNGR_PREFIX via os.environ directly, so no
            # host-env plumbing is needed.
            mngr_command.extend(["--template", "dev"])
        case LaunchMode.LOCAL:
            mngr_command.extend(["--new-host", "--idle-mode", "disabled", "--template", "docker"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.LIMA:
            mngr_command.extend(["--new-host", "--idle-mode", "disabled", "--template", "lima"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.CLOUD:
            mngr_command.extend(["--new-host", "--idle-mode", "disabled", "--template", "vultr"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.LEASED:
            # Unreachable: the first match statement raises for LEASED mode.
            raise MngrCommandError("LEASED mode does not use mngr create -- use the host pool lease flow instead")
        case _ as unreachable:
            assert_never(unreachable)

    if host_env_file is not None:
        mngr_command.extend(["--host-env-file", str(host_env_file)])

    return mngr_command, api_key


def _remote_host_env_flags() -> list[str]:
    """Return the --host-env / --pass-host-env flags for a new remote host.

    Remote containers always store their mngr state under ``/mngr`` (the
    conventional container-internal path -- this is also what
    ``_REMOTE_HOST_DIR`` in ``runner.py`` looks for when writing reverse-tunnel
    API URLs), independent of the local ``MNGR_HOST_DIR`` (which could be
    ``~/.minds/mngr`` or ``~/.devminds/mngr``). We only propagate
    ``MNGR_PREFIX`` so the inner mngr's tmux/session names match the local
    ones, avoiding confusion when the same name has to refer to the "same"
    thing on both sides.
    """
    return [
        "--host-env",
        "MNGR_HOST_DIR=/mngr",
        "--pass-host-env",
        "MNGR_PREFIX",
    ]


def _load_or_create_leased_host_keypair(
    data_dir: Path,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> tuple[Path, str]:
    """Load or generate an SSH keypair for connecting to leased hosts.

    The keypair lives at ``<data_dir>/ssh/keys/leased_host/id_ed25519``. If
    the private key does not exist, ``ssh-keygen`` is invoked to create it.
    Returns ``(private_key_path, public_key_string)``.
    """
    key_dir = data_dir / "ssh" / "keys" / "leased_host"
    key_dir.mkdir(parents=True, exist_ok=True)
    private_key_path = key_dir / "id_ed25519"
    public_key_path = key_dir / "id_ed25519.pub"

    if not private_key_path.exists():
        with log_span("Generating SSH keypair for leased hosts"):
            cg = _make_child_cg("ssh-keygen", parent_cg)
            with cg:
                result = cg.run_process_to_completion(
                    command=[
                        "ssh-keygen",
                        "-t",
                        "ed25519",
                        "-f",
                        str(private_key_path),
                        "-N",
                        "",
                        "-q",
                    ],
                    is_checked_after=False,
                )
            if result.returncode != 0:
                raise MngrCommandError(
                    "ssh-keygen failed (exit code {}): {}".format(
                        result.returncode,
                        result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                    )
                )

    public_key_content = public_key_path.read_text().strip()
    return private_key_path, public_key_content


def _write_dynamic_host_entry(
    dynamic_hosts_file: Path,
    host_name: str,
    address: str,
    port: int,
    user: str,
    key_file: Path,
) -> None:
    """Write or update a host entry in a dynamic hosts TOML file.

    Creates the file and parent directories if they do not exist. Writes
    atomically via a temporary file and rename.
    """
    dynamic_hosts_file.parent.mkdir(parents=True, exist_ok=True)

    if dynamic_hosts_file.exists():
        doc = tomlkit.loads(dynamic_hosts_file.read_text())
    else:
        doc = tomlkit.document()

    # Build the host section
    host_table = tomlkit.table()
    host_table.add("address", address)
    host_table.add("port", port)
    host_table.add("user", user)
    host_table.add("key_file", str(key_file))
    doc[host_name] = host_table

    tmp_path = dynamic_hosts_file.with_suffix(".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.rename(dynamic_hosts_file)


def _remove_dynamic_host_entry(dynamic_hosts_file: Path, host_name: str) -> None:
    """Remove a host entry from a dynamic hosts TOML file.

    No-op if the file does not exist or the host name is not present.
    """
    if not dynamic_hosts_file.exists():
        return

    doc = tomlkit.loads(dynamic_hosts_file.read_text())
    if host_name not in doc:
        return

    del doc[host_name]
    tmp_path = dynamic_hosts_file.with_suffix(".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.rename(dynamic_hosts_file)


def _save_lease_info(data_dir: Path, agent_id: AgentId, host_db_id: UUID) -> None:
    """Persist the lease's host_db_id so release can retrieve it later."""
    lease_dir = data_dir / "leases"
    lease_dir.mkdir(parents=True, exist_ok=True)
    (lease_dir / str(agent_id)).write_text(str(host_db_id))


def _load_lease_info(data_dir: Path, agent_id: AgentId) -> UUID | None:
    """Load the host_db_id for a leased agent, or None if not found."""
    lease_file = data_dir / "leases" / str(agent_id)
    if not lease_file.exists():
        return None
    return UUID(lease_file.read_text().strip())


def _remove_lease_info(data_dir: Path, agent_id: AgentId) -> None:
    """Remove the persisted lease info for an agent."""
    lease_file = data_dir / "leases" / str(agent_id)
    if lease_file.exists():
        lease_file.unlink()


_SEMVER_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^refs/tags/(v\d+\.\d+\.\d+)$")


def resolve_template_version(
    git_url: str,
    branch: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Resolve the template version to use when leasing a host.

    If branch is non-empty, the branch name is the version (dev workflow).
    If branch is empty, uses ``git ls-remote --tags`` to find the latest
    semver tag (e.g. ``v1.2.3``). Falls back to ``"main"`` if no tags found.
    """
    if branch:
        return branch

    cg = _make_child_cg("git-ls-remote-tags", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "ls-remote", "--tags", git_url],
            is_checked_after=False,
        )

    if result.returncode != 0:
        logger.warning("git ls-remote --tags failed for {}, falling back to 'main'", git_url)
        return "main"

    tags: list[tuple[int, int, int, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        ref = parts[1].strip()
        match = _SEMVER_TAG_PATTERN.match(ref)
        if match:
            tag = match.group(1)
            version_parts = tag[1:].split(".")
            tags.append((int(version_parts[0]), int(version_parts[1]), int(version_parts[2]), tag))

    if not tags:
        logger.debug("No semver tags found for {}, falling back to 'main'", git_url)
        return "main"

    tags.sort(reverse=True)
    latest = tags[0][3]
    logger.debug("Resolved latest semver tag for {}: {}", git_url, latest)
    return latest


def run_mngr_create(
    launch_mode: LaunchMode,
    workspace_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    on_output: OutputCallback | None = None,
    host_env_file: Path | None = None,
    latchkey_gateway_url: str | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Create an mngr agent via ``mngr create``.

    The repo's own ``.mngr/settings.toml`` defines agent types, templates,
    environment variables, and all other configuration.

    Returns the generated API key for the agent.
    Raises MngrCommandError if the command fails.
    """
    mngr_command, api_key = _build_mngr_create_command(
        launch_mode,
        agent_name,
        agent_id,
        host_env_file=host_env_file,
        latchkey_gateway_url=latchkey_gateway_url,
    )

    logger.info("Running: {}", " ".join(mngr_command))

    cg = _make_child_cg("mngr-create", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=mngr_command,
            cwd=workspace_dir,
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

    return api_key


class AgentCreator(MutableModel):
    """Creates mngr agents in the background from git repositories or local paths.

    Tracks creation status so the desktop client can show progress
    and redirect users to agents when creation is complete.

    Thread-safe: all status reads/writes are guarded by an internal lock.
    """

    paths: WorkspacePaths = Field(frozen=True, description="Filesystem paths for minds data")
    server_port: int = Field(
        default=0,
        frozen=True,
        description=(
            "Port the desktop client is listening on. Used to build the absolute "
            "http://<agent-id>.localhost:<port>/ redirect URL after agent creation. "
            "The default of 0 is only appropriate for tests that never exercise the "
            "happy-path redirect."
        ),
    )
    host_pool_client: HostPoolClient | None = Field(
        default=None,
        frozen=True,
        description="Client for leasing pre-provisioned hosts from the Vultr host pool",
    )
    litellm_key_client: LiteLLMKeyClient | None = Field(
        default=None,
        frozen=True,
        description="Client for creating LiteLLM virtual keys via the remote connector",
    )
    latchkey_gateway_manager: LatchkeyGatewayManager | None = Field(
        default=None,
        frozen=True,
        description=(
            "Optional gateway manager. When provided, creation pre-spawns a gateway for every "
            "agent and passes the appropriate URL to ``mngr create`` as "
            "``--env LATCHKEY_GATEWAY=...`` so the agent's ``latchkey`` CLI proxies through it. "
            "For DEV agents the URL is the gateway's dynamic host port; for container/VM/VPS "
            "agents it is a constant URL on the agent-side loopback that ``LatchkeyGatewayDiscoveryHandler`` "
            "bridges back via an SSH reverse tunnel once the agent is discovered."
        ),
    )
    root_concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description=(
            "Top-level ``ConcurrencyGroup`` owned by ``start_desktop_client`` and entered for "
            "the duration of the FastAPI lifespan. Every subprocess and thread spawned by this "
            "creator is tracked under it so the desktop-client shutdown can cleanly wait on "
            "(or cancel) in-flight work."
        ),
    )
    notification_dispatcher: NotificationDispatcher = Field(
        frozen=True,
        description=(
            "Dispatcher for surfacing failures from background tasks (e.g. the detached "
            "Cloudflare tunnel setup task) to the user as OS notifications."
        ),
    )

    _statuses: dict[str, AgentCreationStatus] = PrivateAttr(default_factory=dict)
    _redirect_urls: dict[str, str] = PrivateAttr(default_factory=dict)
    _errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _log_queues: dict[str, queue.Queue[str]] = PrivateAttr(default_factory=dict)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _destroy_statuses: dict[str, AgentDestructionStatus] = PrivateAttr(default_factory=dict)
    _destroy_errors: dict[str, str] = PrivateAttr(default_factory=dict)

    def start_creation(
        self,
        repo_source: str,
        agent_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.LOCAL,
        include_env_file: bool = False,
        access_token: str = "",
        version: str = "",
        on_created: Callable[[AgentId], None] | None = None,
    ) -> AgentId:
        """Start creating an agent from a git URL or local path in a background thread.

        When ``include_env_file`` is true and ``repo_source`` resolves to a local
        directory containing a ``.env`` file, that file is passed to ``mngr create``
        via ``--host-env-file`` so local secrets reach the new agent's host.
        The flag is ignored for git URLs (since ``.env`` is gitignored).

        For ``LaunchMode.LEASED``, the host is leased synchronously (fast HTTP
        call) so that the real agent ID from the pool host is used as the
        canonical ID. The remaining setup (rename, start) runs in the background.

        When ``on_created`` is provided, it is called with the agent ID after the
        agent has been successfully created (but before the status is set to DONE).

        Returns the agent ID immediately. Use get_creation_info() to poll status,
        or iter_log_lines() to stream creation logs.
        """
        log_queue: queue.Queue[str] = queue.Queue()
        effective_name = agent_name.strip() if agent_name.strip() else extract_repo_name(repo_source)
        effective_branch = branch.strip()

        lease_result: LeaseHostResult | None = None
        if launch_mode is LaunchMode.LEASED:
            agent_id, lease_result = self._lease_host_synchronously(
                access_token=access_token,
                version=version,
                log_queue=log_queue,
            )
        else:
            agent_id = AgentId()

        with self._lock:
            self._statuses[str(agent_id)] = AgentCreationStatus.CLONING
            self._log_queues[str(agent_id)] = log_queue

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(
                agent_id,
                repo_source,
                effective_name,
                effective_branch,
                log_queue,
                launch_mode,
                include_env_file,
                access_token,
                version,
                on_created,
                lease_result,
            ),
            daemon=True,
            name="agent-creator-{}".format(agent_id),
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)
        return agent_id

    def _lease_host_synchronously(
        self,
        access_token: str,
        version: str,
        log_queue: queue.Queue[str],
    ) -> tuple[AgentId, LeaseHostResult]:
        """Lease a host from the pool and return (agent_id, lease_result).

        Runs synchronously so the caller gets the real agent ID from the
        pool host before setting up status tracking.
        """
        if self.host_pool_client is None:
            raise MngrCommandError("LEASED mode requires a host_pool_client but none is configured")
        if not access_token:
            raise MngrCommandError("LEASED mode requires an access_token for authentication")
        if not version:
            raise MngrCommandError("LEASED mode requires a version string")

        private_key_path, public_key = _load_or_create_leased_host_keypair(
            self.paths.data_dir, parent_cg=self.root_concurrency_group
        )
        log_queue.put("[minds] Requesting a leased host (version: {})...".format(version))
        lease_result = self.host_pool_client.lease_host(access_token, public_key, version)
        logger.debug(
            "Leased host: db_id={}, vps_ip={}, agent_id={}, host_id={}",
            lease_result.host_db_id,
            lease_result.vps_ip,
            lease_result.agent_id,
            lease_result.host_id,
        )
        agent_id = AgentId(lease_result.agent_id)
        return agent_id, lease_result

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

    def release_leased_host(self, agent_id: AgentId, access_token: str) -> None:
        """Release a leased host and clean up local state.

        Removes the dynamic host entry and calls the host pool release endpoint.
        No-op if no lease info is found for the agent.
        """
        host_db_id = _load_lease_info(self.paths.data_dir, agent_id)
        if host_db_id is None:
            logger.debug("No lease info found for agent {}, skipping release", agent_id)
            return

        # Remove the dynamic host entry
        dynamic_hosts_file = self.paths.data_dir / "ssh" / "dynamic_hosts.toml"
        host_name = "leased-{}".format(agent_id)
        _remove_dynamic_host_entry(dynamic_hosts_file, host_name)

        # Call the release endpoint
        if self.host_pool_client is not None:
            is_released = self.host_pool_client.release_host(access_token, host_db_id)
            if is_released:
                _remove_lease_info(self.paths.data_dir, agent_id)
                logger.debug("Released leased host {} for agent {}", host_db_id, agent_id)
            else:
                logger.warning("Failed to release leased host {} for agent {}", host_db_id, agent_id)
        else:
            logger.warning("No host_pool_client configured, cannot release host {}", host_db_id)

    def start_destruction(
        self,
        agent_id: AgentId,
        access_token: str = "",
    ) -> None:
        """Start destroying an agent in a background thread.

        Runs ``mngr destroy``, releases the leased host if applicable,
        and updates the destruction status.
        """
        with self._lock:
            self._destroy_statuses[str(agent_id)] = AgentDestructionStatus.DESTROYING

        thread = threading.Thread(
            target=self._destroy_agent_background,
            args=(agent_id, access_token),
            daemon=True,
            name="agent-destroyer-{}".format(agent_id),
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)

    def get_destruction_info(self, agent_id: AgentId) -> AgentDestructionInfo | None:
        """Get the current destruction status for an agent, or None if not tracked."""
        with self._lock:
            status = self._destroy_statuses.get(str(agent_id))
            if status is None:
                return None
            return AgentDestructionInfo(
                agent_id=agent_id,
                status=status,
                error=self._destroy_errors.get(str(agent_id)),
            )

    def _destroy_agent_background(
        self,
        agent_id: AgentId,
        access_token: str,
    ) -> None:
        """Background thread that destroys all agents on the same host and releases leased resources."""
        aid = str(agent_id)
        try:
            with log_span("Destroying workspace {}", agent_id):
                # Find the host ID and destroy agents BEFORE removing the
                # dynamic host entry -- mngr needs the SSH provider config
                # to discover and destroy agents on the remote host.
                host_id = self._get_host_id_for_agent(agent_id)

                if host_id is not None:
                    self._destroy_all_agents_on_host(host_id)
                else:
                    logger.warning("Could not determine host for agent {}, destroying single agent", agent_id)
                    self._destroy_single_agent(agent_id)

                # Remove the dynamic host entry AFTER destroy so mngr observe
                # stops trying to connect to the now-dead host.
                dynamic_hosts_file = self.paths.data_dir / "ssh" / "dynamic_hosts.toml"
                host_entry_name = "leased-{}".format(agent_id)
                _remove_dynamic_host_entry(dynamic_hosts_file, host_entry_name)

                # Release leased host back to the pool (no-op if not leased)
                if access_token:
                    self.release_leased_host(agent_id, access_token)

                with self._lock:
                    self._destroy_statuses[aid] = AgentDestructionStatus.DONE

        except (MngrCommandError, HostPoolError, ValueError, OSError) as e:
            logger.error("Failed to destroy agent {}: {}", agent_id, e)
            with self._lock:
                self._destroy_statuses[aid] = AgentDestructionStatus.FAILED
                self._destroy_errors[aid] = str(e)

    def _get_host_id_for_agent(self, agent_id: AgentId) -> str | None:
        """Look up the host ID for an agent via ``mngr list``."""
        cg = _make_child_cg("mngr-list-host", self.root_concurrency_group)
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    MNGR_BINARY,
                    "list",
                    "--include",
                    'id == "{}"'.format(agent_id),
                    "--format",
                    "json",
                ],
                is_checked_after=False,
            )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
            agents = data.get("agents", [])
            if agents:
                host = agents[0].get("host", {})
                return host.get("id")
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
        return None

    def _destroy_all_agents_on_host(self, host_id: str) -> None:
        """Destroy all agents on the given host via ``mngr destroy -f``."""
        cg = _make_child_cg("mngr-destroy-host", self.root_concurrency_group)
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    "bash",
                    "-c",
                    "{mngr} list --include 'host.id == \"{host_id}\"' --ids | {mngr} destroy -f -".format(
                        mngr=MNGR_BINARY,
                        host_id=host_id,
                    ),
                ],
                is_checked_after=False,
            )
        if result.returncode != 0:
            raise MngrCommandError(
                "mngr destroy for host {} failed (exit code {}):\n{}".format(
                    host_id,
                    result.returncode,
                    result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                )
            )

    def _destroy_single_agent(self, agent_id: AgentId) -> None:
        """Destroy a single agent via ``mngr destroy``."""
        cg = _make_child_cg("mngr-destroy", self.root_concurrency_group)
        with cg:
            result = cg.run_process_to_completion(
                command=[MNGR_BINARY, "destroy", str(agent_id), "-f"],
                is_checked_after=False,
            )
        if result.returncode != 0:
            raise MngrCommandError(
                "mngr destroy failed for {} (exit code {}):\n{}".format(
                    agent_id,
                    result.returncode,
                    result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
                )
            )

    def _create_agent_background(
        self,
        agent_id: AgentId,
        repo_source: str,
        agent_name: str,
        branch: str,
        log_queue: queue.Queue[str],
        launch_mode: LaunchMode,
        include_env_file: bool,
        access_token: str = "",
        version: str = "",
        on_created: Callable[[AgentId], None] | None = None,
        lease_result: LeaseHostResult | None = None,
    ) -> None:
        """Background thread that resolves the repo source and creates an mngr agent."""
        aid = str(agent_id)
        emit_log = make_log_callback(log_queue)
        host_env_file: Path | None = None
        try:
            if launch_mode is LaunchMode.LEASED:
                if lease_result is None:
                    raise MngrCommandError("LEASED mode requires a lease_result from _lease_host_synchronously")
                self._setup_leased_agent(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    log_queue=log_queue,
                    emit_log=emit_log,
                    access_token=access_token,
                    lease_result=lease_result,
                    on_created=on_created,
                )
                return

            with log_span("Creating agent {} from {} (mode: {})", agent_id, repo_source, launch_mode):
                if _is_local_path(repo_source):
                    resolved_path = Path(os.path.expanduser(repo_source)).resolve()
                    if not resolved_path.is_dir():
                        raise MngrCommandError("Local path does not exist: {}".format(resolved_path))
                    if include_env_file:
                        candidate = resolved_path / ".env"
                        if candidate.is_file():
                            host_env_file = candidate
                            log_queue.put("[minds] Including .env file: {}".format(candidate))
                        else:
                            log_queue.put(
                                "[minds] No .env file found at {}; skipping --host-env-file".format(candidate)
                            )

                    if _is_git_worktree(resolved_path):
                        # Worktrees have a .git file pointing to the parent repo's
                        # .git/worktrees/ dir, which breaks when copied into Docker.
                        # Clone locally to get a standalone repo. Use file:// protocol
                        # so --depth 1 is honored (git ignores --depth for local paths).
                        # Use a stable path based on repo name so Docker layer caching works.
                        log_queue.put("[minds] Cloning local worktree: {}".format(resolved_path))
                        repo_name = extract_repo_name(repo_source)
                        clone_target = Path(tempfile.gettempdir()) / "minds-clone-{}".format(repo_name)
                        if clone_target.exists():
                            shutil.rmtree(clone_target)
                        file_url = GitUrl("file://{}".format(resolved_path))
                        clone_git_repo(
                            file_url,
                            clone_target,
                            on_output=emit_log,
                            is_shallow=True,
                            parent_cg=self.root_concurrency_group,
                        )
                        # The shallow clone only contains committed content. Rsync
                        # the worktree's working directory over so that uncommitted
                        # changes (e.g. a locally-rsynced vendor/mngr/) are included
                        # in the Docker build context.
                        _rsync_worktree_over_clone(
                            resolved_path, clone_target, on_output=emit_log, parent_cg=self.root_concurrency_group
                        )
                        workspace_dir = clone_target
                    else:
                        workspace_dir = resolved_path
                        log_queue.put("[minds] Using local directory: {}".format(workspace_dir))
                else:
                    repo_name = extract_repo_name(repo_source)
                    clone_target = Path(tempfile.gettempdir()) / "minds-clone-{}".format(repo_name)
                    if clone_target.exists():
                        shutil.rmtree(clone_target)
                    log_queue.put("[minds] Cloning {}...".format(repo_source))
                    clone_git_repo(
                        GitUrl(repo_source),
                        clone_target,
                        on_output=emit_log,
                        is_shallow=True,
                        parent_cg=self.root_concurrency_group,
                    )
                    workspace_dir = clone_target

                if branch:
                    log_queue.put("[minds] Checking out branch '{}'...".format(branch))
                    checkout_branch(
                        workspace_dir,
                        GitBranch(branch),
                        on_output=emit_log,
                        parent_cg=self.root_concurrency_group,
                    )

                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.CREATING

                # Pre-spawn a Latchkey gateway for every agent so we can
                # inject ``LATCHKEY_GATEWAY`` at ``mngr create`` time. For
                # container/VM/VPS agents the URL points at a constant
                # agent-side port that is bridged back to the host-side
                # gateway via an SSH reverse tunnel set up on discovery.
                latchkey_gateway_url = self._maybe_start_latchkey_gateway(agent_id, launch_mode, log_queue)

                parsed_name = AgentName(agent_name)
                log_queue.put("[minds] Creating agent '{}' (mode: {})...".format(agent_name, launch_mode.value))
                api_key = run_mngr_create(
                    launch_mode=launch_mode,
                    workspace_dir=workspace_dir,
                    agent_name=parsed_name,
                    agent_id=agent_id,
                    on_output=emit_log,
                    host_env_file=host_env_file,
                    latchkey_gateway_url=latchkey_gateway_url,
                    parent_cg=self.root_concurrency_group,
                )

                # Persist the API key hash
                key_hash = hash_api_key(api_key)
                save_api_key_hash(self.paths.data_dir, agent_id, key_hash)
                log_queue.put("[minds] API key generated and hash stored.")

                log_queue.put("[minds] Agent created successfully.")

                redirect_url = "/goto/{}/".format(agent_id)

                # Set DONE before invoking on_created so the UI can redirect as
                # soon as the agent is usable. ``on_created`` is expected to
                # return quickly (it only schedules background work -- see
                # ``_OnCreatedCallbackFactory``).
                with self._lock:
                    self._statuses[aid] = AgentCreationStatus.DONE
                    self._redirect_urls[aid] = redirect_url

                if on_created is not None:
                    on_created(agent_id)

        except (GitCloneError, GitOperationError, MngrCommandError, HostPoolError, ValueError, OSError) as e:
            logger.error("Failed to create agent {}: {}", agent_id, e)
            log_queue.put("[minds] ERROR: {}".format(e))
            with self._lock:
                self._statuses[aid] = AgentCreationStatus.FAILED
                self._errors[aid] = str(e)
            # A gateway we pre-spawned for this agent is now orphaned (the
            # agent never came into existence), so tear it down to avoid a
            # leaked subprocess + record.
            if self.latchkey_gateway_manager is not None:
                self.latchkey_gateway_manager.stop_gateway_for_agent(agent_id)
        finally:
            log_queue.put(LOG_SENTINEL)

    def _maybe_start_latchkey_gateway(
        self,
        agent_id: AgentId,
        launch_mode: LaunchMode,
        log_queue: queue.Queue[str],
    ) -> str | None:
        """Pre-spawn a Latchkey gateway for this agent and return the URL to inject.

        The URL depends on ``launch_mode``: DEV agents see the gateway on its
        dynamic host port directly; containerized/VM/VPS agents see it on a
        constant port on their own loopback, which is bridged back to the host
        by a reverse SSH tunnel established when the agent is discovered (see
        ``LatchkeyGatewayDiscoveryHandler``).

        Returns ``None`` (and logs a warning) when gateway spawning fails so
        agent creation can still proceed without a gateway URL.
        """
        if self.latchkey_gateway_manager is None:
            return None
        try:
            info = self.latchkey_gateway_manager.ensure_gateway_started(agent_id)
        except LatchkeyGatewayError as e:
            logger.warning("Pre-spawning Latchkey gateway for agent {} failed: {}", agent_id, e)
            log_queue.put(f"[minds] Warning: Latchkey gateway could not be started for this agent: {e}")
            return None
        url = _build_latchkey_gateway_url(launch_mode, info)
        log_queue.put(f"[minds] Latchkey gateway for this agent: {url}")
        return url

    def _setup_leased_agent(
        self,
        agent_id: AgentId,
        agent_name: str,
        log_queue: queue.Queue[str],
        emit_log: OutputCallback,
        access_token: str,
        lease_result: LeaseHostResult,
        on_created: Callable[[AgentId], None] | None = None,
    ) -> None:
        """Set up a leased host (write dynamic host entry, rename, start)."""
        aid = str(agent_id)
        private_key_path = _load_or_create_leased_host_keypair(
            self.paths.data_dir, parent_cg=self.root_concurrency_group
        )[0]

        with log_span("Setting up leased agent {} on {}", agent_id, lease_result.vps_ip):
            with self._lock:
                self._statuses[aid] = AgentCreationStatus.CREATING

            log_queue.put(
                "[minds] Leased host {} (agent: {}, host: {})".format(
                    lease_result.vps_ip, lease_result.agent_id, lease_result.host_id
                )
            )

            dynamic_hosts_file = self.paths.data_dir / "ssh" / "dynamic_hosts.toml"
            host_entry_name = "leased-{}".format(agent_id)
            try:
                _save_lease_info(self.paths.data_dir, agent_id, lease_result.host_db_id)

                _write_dynamic_host_entry(
                    dynamic_hosts_file=dynamic_hosts_file,
                    host_name=host_entry_name,
                    address=lease_result.vps_ip,
                    port=lease_result.container_ssh_port,
                    user=lease_result.ssh_user,
                    key_file=private_key_path,
                )
                log_queue.put("[minds] Dynamic host entry written for {}".format(host_entry_name))

                self._setup_and_start_leased_agent(
                    agent_id=agent_id,
                    aid=aid,
                    agent_name=agent_name,
                    log_queue=log_queue,
                    emit_log=emit_log,
                    access_token=access_token,
                    lease_result=lease_result,
                    on_created=on_created,
                )
            except (MngrCommandError, HostPoolError, LiteLLMKeyError, ValueError, OSError):
                self._cleanup_failed_lease(
                    agent_id=agent_id,
                    access_token=access_token,
                    host_db_id=lease_result.host_db_id,
                    dynamic_hosts_file=dynamic_hosts_file,
                    host_entry_name=host_entry_name,
                    log_queue=log_queue,
                )
                raise

    def _setup_and_start_leased_agent(
        self,
        agent_id: AgentId,
        aid: str,
        agent_name: str,
        log_queue: queue.Queue[str],
        emit_log: OutputCallback,
        access_token: str,
        lease_result: LeaseHostResult,
        on_created: Callable[[AgentId], None] | None = None,
    ) -> None:
        """Create a LiteLLM key, rename, apply labels, inject credentials, and start the leased agent.

        Sequence:
          0. Create a LiteLLM virtual key via the remote connector (network call).
          1. ``mngr rename`` (sequential).
          2. ``mngr label`` + inject MINDS_API_KEY + replace ANTHROPIC_API_KEY
             placeholder + append ANTHROPIC_BASE_URL + patch claude config (parallel).
          3. ``mngr start`` (sequential).

        Raises ``MngrCommandError`` on any failure.
        """
        parsed_name = AgentName(agent_name)
        address = _leased_agent_address(agent_id)

        # Step 0: create a LiteLLM virtual key for this agent
        litellm_key: str | None = None
        litellm_base_url: str | None = None
        if self.litellm_key_client is not None:
            log_queue.put("[minds] Creating LiteLLM virtual key...")
            try:
                key_result = self.litellm_key_client.create_key(
                    access_token=access_token,
                    key_alias=None,
                    max_budget=100.0,
                    budget_duration="1d",
                    metadata={
                        "agent_id": str(agent_id),
                        "host_id": lease_result.host_id,
                    },
                )
                litellm_key = key_result.key
                litellm_base_url = key_result.base_url
                log_queue.put("[minds] LiteLLM virtual key created.")
            except LiteLLMKeyError as e:
                raise MngrCommandError("Failed to create LiteLLM key: {}".format(e)) from e

        api_key = generate_api_key()
        env_path = "/mngr/agents/{}/env".format(agent_id)

        # Build the inject command for MINDS_API_KEY
        inject_minds_key_command = (
            "sed -i '/^MINDS_API_KEY=/d' {path} && echo 'MINDS_API_KEY={key}' >> {path}"
        ).format(path=env_path, key=api_key)

        # Build the inject command for ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL
        inject_anthropic_command: str | None = None
        if litellm_key is not None and litellm_base_url is not None:
            inject_anthropic_command = _build_inject_anthropic_command(
                litellm_key=litellm_key,
                litellm_base_url=litellm_base_url,
                env_path=env_path,
            )

        # Build the command to patch the claude config JSON to approve the new key
        patch_claude_config_command: str | None = None
        if litellm_key is not None:
            patch_claude_config_command = _build_patch_claude_config_command(
                litellm_key=litellm_key,
                agent_id=agent_id,
            )

        cg = _make_child_cg("mngr-leased-setup", self.root_concurrency_group)
        try:
            with cg:
                # Step 1: rename
                log_queue.put("[minds] Renaming agent to '{}'...".format(parsed_name))
                cg.run_process_to_completion(
                    command=[MNGR_BINARY, "rename", address, str(parsed_name)],
                    is_checked_after=True,
                    on_output=emit_log,
                )

                # Step 2: parallel label + inject keys
                log_queue.put("[minds] Applying labels and injecting credentials in parallel...")
                label_proc = cg.run_process_in_background(
                    command=[
                        MNGR_BINARY,
                        "label",
                        address,
                        "-l",
                        "workspace={}".format(parsed_name),
                        "-l",
                        "user_created=true",
                        "-l",
                        "is_primary=true",
                    ],
                    is_checked_by_group=True,
                    on_output=emit_log,
                )
                minds_key_proc = cg.run_process_in_background(
                    command=[MNGR_BINARY, "exec", address, inject_minds_key_command],
                    is_checked_by_group=True,
                    on_output=emit_log,
                )

                anthropic_proc = None
                if inject_anthropic_command is not None:
                    anthropic_proc = cg.run_process_in_background(
                        command=[MNGR_BINARY, "exec", address, inject_anthropic_command],
                        is_checked_by_group=True,
                        on_output=emit_log,
                    )

                claude_config_proc = None
                if patch_claude_config_command is not None:
                    claude_config_proc = cg.run_process_in_background(
                        command=[MNGR_BINARY, "exec", address, patch_claude_config_command],
                        is_checked_by_group=True,
                        on_output=emit_log,
                    )

                label_proc.wait()
                minds_key_proc.wait()
                if anthropic_proc is not None:
                    anthropic_proc.wait()
                if claude_config_proc is not None:
                    claude_config_proc.wait()

                # Step 3: start
                log_queue.put("[minds] Starting agent '{}'...".format(parsed_name))
                cg.run_process_to_completion(
                    command=[MNGR_BINARY, "start", address],
                    is_checked_after=True,
                    on_output=emit_log,
                )
        except (ProcessError, ConcurrencyExceptionGroup) as e:
            raise MngrCommandError("Leased agent setup failed: {}".format(e)) from e

        key_hash = hash_api_key(api_key)
        save_api_key_hash(self.paths.data_dir, agent_id, key_hash)
        log_queue.put("[minds] API key hash stored.")
        log_queue.put("[minds] Leased agent started successfully.")

        port_suffix = ":{}".format(self.server_port) if self.server_port else ""
        redirect_url = "http://{}.localhost{}/".format(agent_id, port_suffix)

        # Set DONE before invoking on_created so the UI can redirect as soon
        # as the agent is actually usable. ``on_created`` is expected to
        # return quickly (it only schedules background work -- see
        # ``_OnCreatedCallbackFactory``), so the try/except wrapper that
        # previously guarded a blocking callback is intentionally gone.
        with self._lock:
            self._statuses[aid] = AgentCreationStatus.DONE
            self._redirect_urls[aid] = redirect_url

        if on_created is not None:
            on_created(agent_id)

    def _cleanup_failed_lease(
        self,
        agent_id: AgentId,
        access_token: str,
        host_db_id: UUID,
        dynamic_hosts_file: Path,
        host_entry_name: str,
        log_queue: queue.Queue[str],
    ) -> None:
        """Best-effort cleanup after a failed leased agent setup.

        Removes the dynamic host entry, releases the host back to the pool,
        and removes the persisted lease info. Logs warnings on cleanup failures
        rather than masking the original error.
        """
        log_queue.put("[minds] Cleaning up after failed lease setup...")

        try:
            _remove_dynamic_host_entry(dynamic_hosts_file, host_entry_name)
        except OSError as cleanup_exc:
            logger.warning("Failed to remove dynamic host entry during cleanup: {}", cleanup_exc)

        if self.host_pool_client is not None and access_token:
            try:
                is_released = self.host_pool_client.release_host(access_token, host_db_id)
                if is_released:
                    logger.debug("Released leased host {} during cleanup", host_db_id)
                else:
                    logger.warning("Failed to release leased host {} during cleanup", host_db_id)
            except (HostPoolError, OSError) as cleanup_exc:
                logger.warning("Error releasing leased host {} during cleanup: {}", host_db_id, cleanup_exc)

        try:
            _remove_lease_info(self.paths.data_dir, agent_id)
        except OSError as cleanup_exc:
            logger.warning("Failed to remove lease info during cleanup: {}", cleanup_exc)
