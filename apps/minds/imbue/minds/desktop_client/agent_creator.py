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
import time
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId

# Inlined to avoid pulling the ``imbue-mngr-forward`` package into minds'
# import graph -- minds spawns the plugin as a subprocess and otherwise has
# no Python-level dependency on it. The constant is a stable wire-format
# contract; if the plugin ever renames its session cookie, both sides update
# together.
_MNGR_FORWARD_SESSION_COOKIE_NAME: Final[str] = "mngr_forward_session"


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
    """Snapshot of agent creation state, returned to callers for status polling.

    The agent creation flow is keyed by ``creation_id`` (a minds-internal
    handle returned synchronously from :py:meth:`AgentCreator.start_creation`)
    because the canonical ``AgentId`` is only known *after* the inner
    ``mngr create`` returns -- for imbue_cloud agents the id is dictated
    by the leased pool host's pre-baked agent, not by minds. ``agent_id``
    is therefore ``None`` until the inner ``mngr create`` emits its
    ``"event": "created"`` JSONL line; consumers that need to redirect
    to ``/goto/<agent_id>/`` should poll ``redirect_url`` instead, which
    is populated atomically with the ``DONE`` status.
    """

    creation_id: CreationId = Field(description="Minds-internal handle for this in-flight creation")
    agent_id: AgentId | None = Field(
        default=None,
        description="Canonical mngr agent id; populated once ``mngr create`` returns, ``None`` while in-flight",
    )
    status: AgentCreationStatus = Field(description="Current creation status")
    redirect_url: str | None = Field(default=None, description="URL to redirect to when creation is done")
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


def _redact_url_credentials(url: str) -> str:
    """Strip any ``user[:password]@`` userinfo from a URL's netloc for logging.

    Used to avoid leaking tokens like ``https://x-access-token:<TOKEN>@...`` into
    debug logs. Strings that urlsplit parses with no netloc userinfo -- local
    paths and SCP-style SSH URLs (``git@github.com:user/repo.git``, which has no
    scheme so urlsplit produces an empty netloc) -- are returned unchanged.
    Schemed URLs that do have userinfo (including ``ssh://git@host/...``) have
    that userinfo stripped; losing the schemed ``user@`` prefix is harmless
    since it isn't a secret and the remaining URL still identifies the repo.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    _, _, host = parts.netloc.rpartition("@")
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


# Matches the ``scheme://user[:password]@`` prefix of a URL embedded anywhere
# in a free-form string (e.g. a line of git's stderr like
# ``fatal: unable to access 'https://x-access-token:TOKEN@github.com/...': ...``).
# Userinfo stops at the first ``/``, ``@``, whitespace, or quote, which are all
# invalid in the unencoded userinfo and reliably terminate it.
_URL_CREDENTIALS_IN_TEXT_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s'\"]+@")


def _redact_url_credentials_in_text(text: str) -> str:
    """Strip ``user[:password]@`` userinfo from any ``scheme://...`` URL inside a string.

    Used to redact credentials from git's streamed stdout/stderr and from
    error messages, which often echo the full URL the user passed in. The
    input is arbitrary text (not a valid URL), so we can't just urlsplit it.
    SCP-style SSH URLs (``git@host:path``, no scheme) are left alone, matching
    :func:`_redact_url_credentials`.
    """
    return _URL_CREDENTIALS_IN_TEXT_RE.sub(r"\1", text)


class _RedactingOutputCallback(FrozenModel):
    """OutputCallback wrapper that scrubs embedded credentials from each line.

    Used by :func:`clone_git_repo` to forward git's streamed stdout/stderr to
    the caller's callback with any ``scheme://user[:password]@...`` URLs
    redacted.
    """

    inner: OutputCallback

    def __call__(self, line: str, is_stdout: bool) -> None:
        self.inner(_redact_url_credentials_in_text(line), is_stdout)


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
    logger.debug("Cloning {} to {}", _redact_url_credentials(str(git_url)), clone_dir)
    command = ["git", "clone"]
    if is_shallow:
        command.extend(["--depth", "1"])
    command.extend([str(git_url), str(clone_dir)])

    # Wrap the caller's on_output so git's per-line stdout/stderr is scrubbed
    # of embedded credentials before being forwarded. Git commonly echoes the
    # full clone URL in error messages (e.g. `fatal: unable to access '...'`),
    # which would otherwise leak tokens from credentialed URLs into logs.
    redacted_on_output = _RedactingOutputCallback(inner=on_output) if on_output is not None else None

    cg = _make_child_cg("git-clone", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
            on_output=redacted_on_output,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise GitCloneError(
            "git clone failed (exit code {}):\n{}".format(
                result.returncode,
                _redact_url_credentials_in_text(stderr if stderr else stdout),
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


def _build_mngr_create_command(
    launch_mode: LaunchMode,
    agent_name: AgentName,
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
) -> tuple[list[str], str]:
    """Build the mngr create command and generate an API key for the agent.

    Returns (command_list, api_key) where api_key is a UUID4 string injected
    as MINDS_API_KEY into the agent's environment via --env. ``--format jsonl``
    is appended so the caller can parse the canonical ``AgentId`` out of
    the trailing ``"event": "created"`` line; minds no longer pre-generates
    an id because for imbue_cloud the lease forces it back to the pool
    host's pre-baked id anyway, and pre-generating one led to bugs (e.g.
    keying gateway state under a fictional id).

    DEV mode: --template main --template dev (runs in-place on local provider)
    LOCAL mode: --template main --template docker (runs in Docker container)
    LIMA mode: --template main --template lima (runs in Lima VM)
    CLOUD mode: --template main --template vultr (runs in Docker on a Vultr VPS)
    IMBUE_CLOUD mode: --new-host on the imbue_cloud_<slug> provider (the
        plugin's create_host adopts the pool's pre-baked agent under
        ``agent_name``); ``imbue_cloud_*`` arguments encode the lease
        attributes (--build-arg).

    For modes that create a separate host (LOCAL, LIMA, CLOUD, IMBUE_CLOUD),
    the agent address uses ``agent_name@{agent_name}-host`` so hosts are
    clearly attributable. ``--reuse`` and ``--update`` are passed for the
    non-IMBUE_CLOUD modes so re-deploying resets the agent on the same
    host instead of failing on a duplicate name (IMBUE_CLOUD's lease
    flow is one-shot per pool host, so reuse is not meaningful there).

    Secrets (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``, ``GH_TOKEN``)
    are forwarded by the FCT template's own ``pass_(host_)env`` declarations,
    not by inline flags here -- ``run_mngr_create`` populates them in the
    subprocess env when needed and the template-declared forwards pick
    them up. Keeping the forwarding declaration in FCT means the same
    template works for ``mngr create`` invocations from outside minds too.

    For container/VM/VPS/leased modes, ``LATCHKEY_GATEWAY=http://127.0.0.1:<port>``
    is injected unconditionally; the agent reaches that loopback URL via the
    reverse tunnel that ``LatchkeyDiscoveryHandler`` sets up post-discovery.
    DEV mode gets no injected ``LATCHKEY_GATEWAY`` -- DEV runs on the bare
    host without a reverse tunnel, so any test that wants latchkey there
    should set the env var itself.
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
        case LaunchMode.IMBUE_CLOUD:
            if not imbue_cloud_account:
                raise MngrCommandError("IMBUE_CLOUD mode requires imbue_cloud_account")
            slug = _slugify_account(imbue_cloud_account)
            address = f"{agent_name}@{_make_host_name(agent_name)}.imbue_cloud_{slug}"
        case _ as unreachable:
            assert_never(unreachable)

    api_key = generate_api_key()

    # The `/welcome` initial message is now baked into the FCT template's
    # [create_templates.main] section, so we no longer pass `--message` here.
    # ``--format jsonl`` makes mngr emit ``{"event": "created", "agent_id": ..., "host_id": ...}``
    # as the final stdout line; ``run_mngr_create`` parses that to recover
    # the canonical agent id.
    # Non-DEV modes bridge the agent's loopback to a host-side latchkey
    # gateway via the reverse tunnel ``LatchkeyDiscoveryHandler`` sets up
    # post-discovery, so the URL is always the constant agent-side port.
    # DEV mode runs the agent on the bare host with no tunnel; tests there
    # set ``LATCHKEY_GATEWAY`` themselves if they want one.
    latchkey_env_args: list[str] = []
    if launch_mode is not LaunchMode.DEV:
        latchkey_env_args = ["--env", f"LATCHKEY_GATEWAY=http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"]

    mngr_command: list[str] = [
        MNGR_BINARY,
        "create",
        address,
        "--no-connect",
        "--format",
        "jsonl",
        "--label",
        f"workspace={agent_name}",
        "--env",
        f"MINDS_API_KEY={api_key}",
        "--label",
        "user_created=true",
        *latchkey_env_args,
        "--label",
        "is_primary=true",
    ]

    match launch_mode:
        case LaunchMode.IMBUE_CLOUD:
            # Each lease is one-shot, so --reuse / --update would be confusing.
            # The id is dictated by the pool's pre-baked agent and read back
            # from the JSONL "created" event below.
            pass
        case _:
            mngr_command.extend(["--reuse", "--update"])

    # Per-mode template + per-mode runtime flags. All modes use
    # ``--template main --template <mode>``; the per-mode template provides
    # the provider-specific knobs (idle_mode, pass_host_env, build_arg, ...)
    # while runtime-only knobs that vary per-invocation (``--new-host``,
    # ``-b lease_attributes``) stay inline.
    match launch_mode:
        case LaunchMode.DEV:
            # Local (same-machine) mode: the agent inherits the bootstrap-set
            # MNGR_HOST_DIR/MNGR_PREFIX via os.environ directly, so no
            # host-env plumbing is needed.
            mngr_command.extend(["--template", "main", "--template", "dev"])
        case LaunchMode.LOCAL:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "docker"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.LIMA:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "lima"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.CLOUD:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "vultr"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.IMBUE_CLOUD:
            # imbue_cloud follows the same shape as the other modes: the
            # ``main`` + ``imbue_cloud`` templates set ``idle_mode = disabled``
            # + ``pass_host_env`` for the LiteLLM creds, and the runtime-only
            # lease-attribute ``-b`` flags stay inline because they vary per
            # invocation.
            mngr_command.extend(["--new-host", "--template", "main", "--template", "imbue_cloud"])
            if imbue_cloud_repo_url:
                mngr_command.extend(["-b", f"repo_url={imbue_cloud_repo_url}"])
            if imbue_cloud_branch_or_tag:
                mngr_command.extend(["-b", f"repo_branch_or_tag={imbue_cloud_branch_or_tag}"])
        case _ as unreachable:
            assert_never(unreachable)

    return mngr_command, api_key


def _slugify_account(account: str) -> str:
    """Mirror ``slugify_account`` from the plugin so the provider instance name lines up.

    Inlined (rather than imported from ``imbue.mngr_imbue_cloud``) because minds
    invokes ``mngr`` as a subprocess and is not allowed to depend on the
    plugin Python API.
    """
    lowered = account.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise MngrCommandError(f"Cannot slugify imbue_cloud account email: {account!r}")
    return slug


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


class _CreateEventCapture(MutableModel):
    """Forwards each child-process line to ``on_output`` while sniffing for ``mngr create``'s JSONL ``created`` event.

    ``mngr create --format jsonl`` writes structured event records to stdout
    -- the final one being ``{"event": "created", "agent_id": "...", "host_id": "..."}``.
    Each line still goes through to the caller's ``on_output`` so log
    streaming behaviour is unchanged; this wrapper just records the
    canonical agent id when it sees the matching event so the caller can
    return it without a follow-up ``mngr list`` lookup.
    """

    inner_on_output: OutputCallback | None = Field(
        default=None,
        description="Caller's per-line callback that gets every stdout/stderr line, regardless of parsing",
    )
    canonical_agent_id: AgentId | None = Field(
        default=None,
        description="Populated when a JSONL ``created`` event is seen on stdout",
    )
    canonical_host_id: str | None = Field(
        default=None,
        description="Populated alongside ``canonical_agent_id`` from the same JSONL event",
    )

    def __call__(self, line: str, is_stdout: bool) -> None:
        if self.inner_on_output is not None:
            self.inner_on_output(line, is_stdout)
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict) or event.get("event") != "created":
            return
        agent_id_raw = event.get("agent_id")
        if isinstance(agent_id_raw, str) and agent_id_raw:
            self.canonical_agent_id = AgentId(agent_id_raw)
        host_id_raw = event.get("host_id")
        if isinstance(host_id_raw, str) and host_id_raw:
            self.canonical_host_id = host_id_raw


def run_mngr_create(
    launch_mode: LaunchMode,
    workspace_dir: Path | None,
    agent_name: AgentName,
    on_output: OutputCallback | None = None,
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
    anthropic_api_key: str | None = None,
    anthropic_base_url: str | None = None,
    gh_token: str | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> tuple[str, AgentId]:
    """Create an mngr agent via ``mngr create --format jsonl``.

    The repo's own ``.mngr/settings.toml`` defines agent types, templates,
    environment variables, and all other configuration. ``workspace_dir`` is
    the cwd the subprocess runs in (so ``mngr create`` picks up the local
    repo's ``.mngr/`` settings); IMBUE_CLOUD passes ``None`` because the
    pool host has its own pre-baked ``.mngr/`` and the local repo is
    irrelevant.

    ``anthropic_api_key`` / ``anthropic_base_url`` / ``gh_token`` are placed
    into the subprocess env (not argv) so they don't show up in ``ps`` output;
    the FCT template's own ``pass_(host_)env`` declarations cause mngr to
    forward them onto the host (or the DEV agent) as appropriate.

    Returns ``(api_key, canonical_agent_id)``. The canonical id is parsed
    out of the ``"event": "created"`` JSONL line that ``mngr create``
    emits as its final stdout record.

    Raises ``MngrCommandError`` if the command fails or never emits a
    ``created`` event (e.g. crashed before final-output stage).
    """
    mngr_command, api_key = _build_mngr_create_command(
        launch_mode,
        agent_name,
        imbue_cloud_account=imbue_cloud_account,
        imbue_cloud_repo_url=imbue_cloud_repo_url,
        imbue_cloud_branch_or_tag=imbue_cloud_branch_or_tag,
    )

    # Build the subprocess env from the parent's env + any secrets we inject
    # for the matching ``--pass-(host-)env`` flag to forward. Mutating
    # ``os.environ`` directly would leak the user's secrets into the desktop
    # client's other subprocesses, so we keep the override scoped to this
    # invocation.
    subprocess_env: dict[str, str] | None = None
    if anthropic_api_key is not None or anthropic_base_url is not None or gh_token is not None:
        subprocess_env = dict(os.environ)
        if anthropic_api_key is not None:
            subprocess_env["ANTHROPIC_API_KEY"] = anthropic_api_key
        if anthropic_base_url is not None:
            subprocess_env["ANTHROPIC_BASE_URL"] = anthropic_base_url
        if gh_token is not None:
            subprocess_env["GH_TOKEN"] = gh_token

    logger.info("Running: {}", " ".join(mngr_command))

    capture = _CreateEventCapture(inner_on_output=on_output)
    cg = _make_child_cg("mngr-create", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=mngr_command,
            cwd=workspace_dir,
            is_checked_after=False,
            on_output=capture,
            env=subprocess_env,
        )

    if result.returncode != 0:
        raise MngrCommandError(
            "mngr create failed (exit code {}):\n{}".format(
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )

    if capture.canonical_agent_id is None:
        # Exit-zero without a created event almost certainly means the
        # JSONL output got mangled or some pre-emit error path took over.
        # Fail loudly rather than fall through with a sentinel id.
        raise MngrCommandError(
            "mngr create exited 0 but did not emit a JSONL 'created' event; stdout tail:\n{}".format(
                result.stdout.strip()[-2000:]
            )
        )

    return api_key, capture.canonical_agent_id


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
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        default=None,
        frozen=True,
        description=(
            "Wrapper around `mngr imbue_cloud …`. Used by IMBUE_CLOUD-mode creations to mint "
            "a LiteLLM virtual key before the standard ``mngr create`` invocation, and by "
            "destruction to release the lease. The lease + SSH bootstrap + agent rename "
            "themselves run inside the plugin's ``ImbueCloudProvider.create_host``, so minds "
            "no longer maintains its own SuperTokens session, host pool, or LiteLLM key code. "
            "Other launch modes do not consult this client."
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
    mngr_forward_port: int = Field(
        default=0,
        frozen=True,
        description=(
            "Port the ``mngr forward`` plugin is bound to. Used by ``_wait_for_workspace_ready`` to "
            "probe the freshly-created agent's workspace_server through the plugin's per-subdomain "
            "endpoint before publishing the redirect URL. The default of 0 disables readiness "
            "probing -- only appropriate for tests that never exercise the happy-path redirect."
        ),
    )
    mngr_forward_preauth_cookie: str = Field(
        default="",
        frozen=True,
        description=(
            "Pre-shared ``mngr_forward_session`` cookie value. Sent on readiness probes so the plugin "
            "treats them as authenticated without requiring the OTP-issued cookie. Empty disables "
            "readiness probing alongside ``mngr_forward_port=0``."
        ),
    )
    workspace_ready_timeout_seconds: float = Field(
        default=60.0,
        frozen=True,
        description="Maximum time to wait for the new agent's workspace_server to return HTTP 200.",
    )
    workspace_ready_poll_interval_seconds: float = Field(
        default=0.5,
        frozen=True,
        description="Sleep between probe attempts when the workspace_server is not yet ready.",
    )
    workspace_ready_probe_timeout_seconds: float = Field(
        default=2.0,
        frozen=True,
        description="Per-request timeout for the readiness probe HTTP GET.",
    )

    # In-flight creation state is keyed by ``str(CreationId)`` because the
    # canonical ``AgentId`` doesn't exist until ``mngr create`` returns.
    # Once it does, the corresponding ``CreationId`` row in
    # ``_canonical_agent_ids`` gets populated and ``AgentCreationInfo``
    # snapshots include the new ``agent_id`` field.
    _statuses: dict[str, AgentCreationStatus] = PrivateAttr(default_factory=dict)
    _canonical_agent_ids: dict[str, AgentId] = PrivateAttr(default_factory=dict)
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
        ai_provider: AIProvider = AIProvider.SUBSCRIPTION,
        account_email: str = "",
        branch_or_tag: str = "",
        anthropic_api_key: str = "",
        gh_token: str = "",
        on_created: Callable[[AgentId], None] | None = None,
    ) -> CreationId:
        """Start creating an agent from a git URL or local path in a background thread.

        ``ai_provider`` controls how the agent obtains its Anthropic
        credentials, decoupled from the compute provider:

        - ``IMBUE_CLOUD`` -- mint a LiteLLM virtual key against
          ``account_email`` and inject ``ANTHROPIC_API_KEY`` /
          ``ANTHROPIC_BASE_URL`` so the agent talks to LiteLLM. Requires
          an account.
        - ``API_KEY`` -- inject ``anthropic_api_key`` directly so the agent
          talks to the official Anthropic API.
        - ``SUBSCRIPTION`` -- inject neither; the user signs in to Claude
          interactively in the workspace.

        ``gh_token`` is optional; when provided it's forwarded to the host
        (or the agent in DEV mode) as ``GH_TOKEN``.

        For ``LaunchMode.IMBUE_CLOUD``, the agent runs on a leased pool host
        via the ``imbue_cloud_<account-slug>`` provider; the plugin's
        ``ImbueCloudProvider.create_host`` runs the lease + SSH bootstrap
        and the rest of mngr's create pipeline adopts the pool host's
        pre-baked agent under the requested name. The plugin owns the
        SuperTokens session, so minds only needs to know which account to
        ask for.

        When ``on_created`` is provided, it is called with the canonical
        ``AgentId`` once ``mngr create`` returns (immediately before the
        status flips to ``DONE``). The id is parsed from the inner
        ``mngr create``'s JSONL ``"event": "created"`` line, not pre-generated;
        for imbue_cloud agents it's the leased pool host's pre-baked id.

        Returns a ``CreationId`` immediately for tracking the in-flight
        creation. Use ``get_creation_info()`` to poll status (and read
        ``info.agent_id`` once it's populated) or ``get_log_queue()`` to
        stream creation logs. The minds-internal ``CreationId`` and the
        canonical ``AgentId`` are different namespaces by design (different
        ``RandomId`` prefixes) so they can never accidentally be swapped.
        """
        log_queue: queue.Queue[str] = queue.Queue()
        effective_name = agent_name.strip() if agent_name.strip() else extract_repo_name(repo_source)
        effective_branch = branch.strip()

        creation_id = CreationId()

        with self._lock:
            self._statuses[str(creation_id)] = AgentCreationStatus.CLONING
            self._log_queues[str(creation_id)] = log_queue

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(
                creation_id,
                repo_source,
                effective_name,
                effective_branch,
                log_queue,
                launch_mode,
                ai_provider,
                account_email,
                branch_or_tag,
                anthropic_api_key,
                gh_token,
                on_created,
            ),
            daemon=True,
            name="agent-creator-{}".format(creation_id),
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)
        return creation_id

    def wait_for_all(self, timeout: float = 10.0) -> None:
        """Wait for all background creation threads to finish."""
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=timeout)

    def get_creation_info(self, creation_id: CreationId) -> AgentCreationInfo | None:
        """Get the current creation status for an in-flight creation, or None if not tracked.

        ``info.agent_id`` is ``None`` until the inner ``mngr create``
        returns and emits its JSONL ``"event": "created"`` line, after
        which it's populated with the canonical mngr id. ``info.redirect_url``
        is populated atomically with ``DONE``, so the UI doesn't need to
        wait for ``agent_id`` to know where to redirect.
        """
        cid_str = str(creation_id)
        with self._lock:
            status = self._statuses.get(cid_str)
            if status is None:
                return None
            return AgentCreationInfo(
                creation_id=creation_id,
                agent_id=self._canonical_agent_ids.get(cid_str),
                status=status,
                redirect_url=self._redirect_urls.get(cid_str),
                error=self._errors.get(cid_str),
            )

    def get_log_queue(self, creation_id: CreationId) -> queue.Queue[str] | None:
        """Get the log queue for an in-flight creation, or None if not tracked."""
        with self._lock:
            return self._log_queues.get(str(creation_id))

    def _create_agent_background(
        self,
        creation_id: CreationId,
        repo_source: str,
        agent_name: str,
        branch: str,
        log_queue: queue.Queue[str],
        launch_mode: LaunchMode,
        ai_provider: AIProvider,
        account_email: str = "",
        branch_or_tag: str = "",
        anthropic_api_key: str = "",
        gh_token: str = "",
        on_created: Callable[[AgentId], None] | None = None,
    ) -> None:
        """Background thread that resolves the repo source and creates an mngr agent.

        For ``ai_provider == IMBUE_CLOUD``, mints a LiteLLM key (via the
        plugin CLI) and forwards ``ANTHROPIC_API_KEY``/``ANTHROPIC_BASE_URL``
        onto the host (or DEV agent) via the subprocess env + matching
        ``--pass-(host-)env`` flags. For ``API_KEY``, forwards the
        user-supplied key as ``ANTHROPIC_API_KEY``. For ``SUBSCRIPTION``,
        injects neither.

        For ``LaunchMode.IMBUE_CLOUD``, the plugin's provider backend
        handles the lease + SSH bootstrap inside ``create_host``; the
        canonical agent id is parsed from ``mngr create``'s JSONL
        ``"event": "created"`` line (no follow-up ``mngr list`` lookup --
        which used to fail when the SSH provider had stale dynamic_hosts
        entries).
        """
        cid_str = str(creation_id)
        emit_log = make_log_callback(log_queue)
        workspace_dir: Path | None = None
        try:
            with log_span(
                "Creating agent for creation {} from {} (mode: {})",
                creation_id,
                _redact_url_credentials(repo_source),
                launch_mode,
            ):
                # Resolve / clone the repo locally for *every* launch mode so
                # ``mngr create``'s cwd is a checkout of the template repo
                # (which has the ``[create_templates.<mode>]`` blocks). For
                # IMBUE_CLOUD this clone is "wasted" in the sense that the
                # leased pool host has its own pre-baked checkout, but it's
                # what gives the local mngr a place to read the per-mode
                # template + agent_types from -- the alternative was minds
                # inlining all those flags as command-line args, which let
                # the imbue_cloud command-construction drift from the other
                # modes' (and was hard to keep in sync with the bake's view
                # of the same config).
                if _is_local_path(repo_source):
                    resolved_path = Path(os.path.expanduser(repo_source)).resolve()
                    if not resolved_path.is_dir():
                        raise MngrCommandError("Local path does not exist: {}".format(resolved_path))

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
                            resolved_path,
                            clone_target,
                            on_output=emit_log,
                            parent_cg=self.root_concurrency_group,
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
                    log_queue.put("[minds] Cloning {}...".format(_redact_url_credentials(repo_source)))
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

                # Resolve the Anthropic credentials according to the AI
                # provider choice. IMBUE_CLOUD mints a fresh LiteLLM key;
                # API_KEY uses the user-supplied key directly; SUBSCRIPTION
                # injects nothing so the agent prompts the user to log in.
                effective_anthropic_api_key: str | None = None
                effective_anthropic_base_url: str | None = None
                match ai_provider:
                    case AIProvider.IMBUE_CLOUD:
                        if self.imbue_cloud_cli is None:
                            raise MngrCommandError("AI provider IMBUE_CLOUD requires imbue_cloud_cli to be configured")
                        if not account_email:
                            raise MngrCommandError("AI provider IMBUE_CLOUD requires an account_email to be supplied")
                        log_queue.put(f"[minds] Minting LiteLLM virtual key for account {account_email}...")
                        try:
                            key_material: LiteLLMKeyMaterial = self.imbue_cloud_cli.create_litellm_key(
                                account=account_email,
                                alias=None,
                                max_budget=100.0,
                                budget_duration="1d",
                                metadata={"agent_name": agent_name},
                            )
                        except ImbueCloudCliError as exc:
                            raise MngrCommandError(f"Failed to create LiteLLM key: {exc}") from exc
                        log_queue.put("[minds] LiteLLM key minted.")
                        effective_anthropic_api_key = key_material.key.get_secret_value()
                        effective_anthropic_base_url = str(key_material.base_url)
                    case AIProvider.API_KEY:
                        if not anthropic_api_key:
                            raise MngrCommandError("AI provider API_KEY requires anthropic_api_key to be supplied")
                        effective_anthropic_api_key = anthropic_api_key
                    case AIProvider.SUBSCRIPTION:
                        pass
                    case _ as unreachable:
                        assert_never(unreachable)

                with self._lock:
                    self._statuses[cid_str] = AgentCreationStatus.CREATING

                parsed_name = AgentName(agent_name)
                log_queue.put("[minds] Creating agent '{}' (mode: {})...".format(agent_name, launch_mode.value))
                api_key, canonical_id = run_mngr_create(
                    launch_mode=launch_mode,
                    workspace_dir=workspace_dir,
                    agent_name=parsed_name,
                    on_output=emit_log,
                    imbue_cloud_account=account_email if launch_mode is LaunchMode.IMBUE_CLOUD else None,
                    # Don't constrain the lease on ``repo_url`` here:
                    # ``repo_source`` is whatever the user picked in the UI
                    # (often a local FCT clone path), but pool hosts are
                    # operator-baked with whatever ``--attributes`` JSON the
                    # admin chose -- typically ``cpus``/``memory_gb``/
                    # ``repo_branch_or_tag`` and not ``repo_url``. Including
                    # ``repo_url`` here would make every lease request fail
                    # the JSONB ``@>`` match. Constraining on
                    # ``repo_branch_or_tag`` (when minds knows it) is enough
                    # to pick the right pool generation.
                    imbue_cloud_branch_or_tag=(
                        branch_or_tag if launch_mode is LaunchMode.IMBUE_CLOUD and branch_or_tag else None
                    ),
                    anthropic_api_key=effective_anthropic_api_key,
                    anthropic_base_url=effective_anthropic_base_url,
                    gh_token=gh_token if gh_token else None,
                    parent_cg=self.root_concurrency_group,
                )

                # Persist the API key hash under the canonical id so future
                # ``/api/<agent_id>`` requests authenticate against it.
                key_hash = hash_api_key(api_key)
                save_api_key_hash(self.paths.data_dir, canonical_id, key_hash)
                log_queue.put("[minds] API key generated and hash stored.")

                log_queue.put("[minds] Agent created successfully.")

                # Wait for the agent's workspace_server to actually answer 200
                # through the plugin before publishing the redirect. Without
                # this poll, the user gets dropped on a hard error page (404
                # /503) for the few seconds between ``mngr create`` returning
                # and the workspace_server inside the agent finishing
                # startup. The probe is best-effort: if it times out, we
                # publish anyway so the user at least lands on the retry
                # page rather than spinning forever (PR 1471 part 1).
                self._wait_for_workspace_ready(canonical_id, log_queue)

                # The redirect URL is *absolute* and points at the plugin's
                # bare origin. ``creating.js`` does
                # ``window.location.href = data.redirect_url`` directly; a
                # relative ``/goto/...`` would navigate to the minds origin
                # (port :8420) where ``/goto/`` is unrouted -- the user
                # would land on FastAPI's default ``{"detail":"Not Found"}``
                # response instead of being bridged into the agent
                # subdomain. The plugin owns ``/goto/<agent>/``.
                redirect_url = self._build_redirect_url(canonical_id)

                # Publish the canonical id + DONE atomically so the UI sees
                # both at once. ``on_created`` runs after publication so any
                # downstream consumer (e.g. ``_OnCreatedCallbackFactory``,
                # which kicks off the Cloudflare tunnel + workspace
                # association) can rely on the canonical id.
                with self._lock:
                    self._canonical_agent_ids[cid_str] = canonical_id
                    self._statuses[cid_str] = AgentCreationStatus.DONE
                    self._redirect_urls[cid_str] = redirect_url

                if on_created is not None:
                    on_created(canonical_id)

        except (GitCloneError, GitOperationError, MngrCommandError, ImbueCloudCliError, ValueError, OSError) as e:
            logger.opt(exception=e).error("Failed to create agent for creation {}", creation_id)
            log_queue.put("[minds] ERROR: {}".format(e))
            with self._lock:
                self._statuses[cid_str] = AgentCreationStatus.FAILED
                self._errors[cid_str] = str(e)
        finally:
            log_queue.put(LOG_SENTINEL)

    def _build_redirect_url(self, agent_id: AgentId) -> str:
        """Build the absolute URL the UI should navigate to after creation.

        Always points at the plugin's ``/goto/<agent>/`` route, never minds'
        bare origin -- minds doesn't serve ``/goto/`` and would 404. When
        ``mngr_forward_port`` isn't configured (test fixtures, etc.), falls
        back to the relative form so legacy callers that don't set the field
        keep working.
        """
        if self.mngr_forward_port == 0:
            return f"/goto/{agent_id}/"
        return f"http://localhost:{self.mngr_forward_port}/goto/{agent_id}/"

    def _wait_for_workspace_ready(self, agent_id: AgentId, log_queue: queue.Queue[str]) -> None:
        """Poll the agent's workspace_server through the plugin until it responds 200.

        Probes ``http://<agent_id>.localhost:<plugin_port>/`` with the preauth
        cookie set, treating any 200 as ready. Other status codes (typically
        503 from the plugin's auto-refresh page when the workspace_server
        isn't yet listening, or 502 when SSH info hasn't propagated) are
        treated as not-yet-ready and re-polled until the timeout elapses.

        Best-effort: if probing is unconfigured (``mngr_forward_port=0`` or
        empty preauth, e.g. tests that bypass the plugin) we return immediately.
        On timeout we log + emit to the log queue and let the caller publish
        the redirect anyway -- the user lands on the plugin's auto-refresh
        retry page, which is better than spinning forever in the creation UI.
        """
        if self.mngr_forward_port == 0 or not self.mngr_forward_preauth_cookie:
            logger.debug("Workspace readiness probe disabled (port=0 or empty preauth); skipping")
            return

        probe_url = f"http://{agent_id}.localhost:{self.mngr_forward_port}/"
        deadline = time.monotonic() + self.workspace_ready_timeout_seconds
        log_queue.put("[minds] Waiting for workspace server to be ready...")
        last_status: int | None = None
        last_error: str | None = None
        attempt = 0
        with httpx.Client(
            timeout=self.workspace_ready_probe_timeout_seconds,
            follow_redirects=False,
            cookies={_MNGR_FORWARD_SESSION_COOKIE_NAME: self.mngr_forward_preauth_cookie},
        ) as client:
            while time.monotonic() < deadline:
                attempt += 1
                try:
                    response = client.get(probe_url)
                    last_status = response.status_code
                    if response.status_code == 200:
                        logger.debug(
                            "Workspace ready for {} after {} probe(s)",
                            agent_id,
                            attempt,
                        )
                        log_queue.put("[minds] Workspace server is ready.")
                        return
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as e:
                    last_error = f"{type(e).__name__}: {e}"
                threading.Event().wait(timeout=self.workspace_ready_poll_interval_seconds)
        logger.warning(
            "Workspace readiness probe for {} timed out after {:.0f}s "
            "(last status={}, last error={}); publishing redirect anyway",
            agent_id,
            self.workspace_ready_timeout_seconds,
            last_status,
            last_error,
        )
        log_queue.put(
            "[minds] Warning: workspace did not become ready within "
            f"{self.workspace_ready_timeout_seconds:.0f}s; you may see a retry page on first load."
        )
