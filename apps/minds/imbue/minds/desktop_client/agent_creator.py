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
from collections.abc import Mapping
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
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import SYSTEM_SERVICES_AGENT_NAME
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.utils.git_utils import rsync_worktree_over_clone
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import finalize_host_permissions
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyStoreError

# Inlined to avoid pulling the ``imbue-mngr-forward`` package into minds'
# import graph -- minds spawns the plugin as a subprocess and otherwise has
# no Python-level dependency on it. The constant is a stable wire-format
# contract; if the plugin ever renames its session cookie, both sides update
# together.
_MNGR_FORWARD_SESSION_COOKIE_NAME: Final[str] = "mngr_forward_session"

# Path the workspace-readiness / health probes hit through the plugin. We probe
# ``/`` and treat any 200 as "ready" -- deliberately *not* coupled to any
# particular application running inside the workspace. The probe only confirms
# that some web server is up and answering on the inner port; it makes no
# assumption about which app that is or which routes it implements.
_WORKSPACE_PROBE_PATH: Final[str] = "/"


def make_workspace_probe_client(preauth_cookie: str, probe_timeout_seconds: float) -> httpx.Client:
    """Construct a reusable httpx.Client preconfigured for workspace probes.

    Callers that probe in a tight poll loop should construct one of these and
    pass it to ``probe_workspace_through_plugin`` on each iteration, instead
    of letting the helper construct a one-shot client per call.
    """
    return httpx.Client(
        timeout=probe_timeout_seconds,
        follow_redirects=False,
        cookies={_MNGR_FORWARD_SESSION_COOKIE_NAME: preauth_cookie},
    )


def _probe_once(probe_client: httpx.Client, probe_url: str, host_header: str) -> int | None:
    """Issue a single GET through ``probe_client`` and return the status code.

    ``probe_url`` targets loopback directly; ``host_header`` carries the
    ``agent-<hex>.localhost`` vhost the plugin routes on. Sending the subdomain
    as an explicit ``Host`` header rather than in the URL keeps the probe from
    depending on ``*.localhost`` name resolution, which is not available on a
    bare Linux host (only loopback ``localhost`` itself reliably resolves).

    Returns ``None`` if the probe failed at the transport layer (connect
    error, mid-stream EOF, read timeout). Module-private helper used by
    ``probe_workspace_through_plugin``; hoisted out to satisfy the minds
    project's no-inner-functions ratchet.
    """
    try:
        response = probe_client.get(probe_url, headers={"Host": host_header})
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException):
        return None
    return response.status_code


def probe_workspace_through_plugin(
    mngr_forward_port: int,
    preauth_cookie: str,
    agent_id: AgentId,
    probe_timeout_seconds: float,
    client: httpx.Client | None = None,
) -> int | None:
    """Issue a single probe through the plugin to the agent's inner web server.

    Probes ``/`` (see ``_WORKSPACE_PROBE_PATH``). Returns the HTTP status code
    observed (a 200 means some web server is up and answering on the inner
    port), or ``None`` if the probe failed at the transport layer (connect
    error, mid-stream EOF, read timeout). Shared by ``_wait_for_workspace_ready``
    (creation flow) and the system-interface-health tracker's background
    probe loop so both paths agree on what "ready" means.

    Pass a pre-constructed ``client`` (via ``make_workspace_probe_client``)
    to reuse the connection pool across a tight poll loop. When omitted, a
    one-shot client is constructed for this single probe -- fine for
    one-off / sporadic callers but wasteful in a loop.
    """
    probe_url = f"http://127.0.0.1:{mngr_forward_port}{_WORKSPACE_PROBE_PATH}"
    host_header = f"{agent_id}.localhost"
    if client is not None:
        return _probe_once(client, probe_url, host_header)
    with make_workspace_probe_client(
        preauth_cookie=preauth_cookie, probe_timeout_seconds=probe_timeout_seconds
    ) as one_shot:
        return _probe_once(one_shot, probe_url, host_header)


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
    """Status of a background agent creation.

    The non-terminal values correspond to the ordered phases the worker
    thread walks through; ``_stream_creation_logs`` polls the current
    status and emits a SSE event each time it changes so the UI spinner
    caption stays in sync with what the backend is actually doing.
    Conditional phases (``CHECKING_OUT_BRANCH`` only if a branch was
    given, ``PROVISIONING_AI`` only for ``IMBUE_CLOUD`` AI provider) are
    skipped when they don't apply -- the status simply jumps to the next
    applicable phase.
    """

    INITIALIZING = auto()
    CLONING_REPO = auto()
    CHECKING_OUT_BRANCH = auto()
    PROVISIONING_AI = auto()
    CREATING_WORKSPACE = auto()
    WAITING_FOR_READY = auto()
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
    launch_mode: LaunchMode = Field(
        description=(
            "Launch mode for this creation. Carried alongside status so consumers can resolve "
            "mode-aware status captions without a separate lookup."
        ),
    )
    host_name: str = Field(
        default="",
        description=(
            "Resolved workspace/host name for this creation (the form's Name field, or a "
            "repo-derived fallback). Carried alongside status as creation metadata."
        ),
    )
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
    branch: GitBranch | None = None,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Clone a git repository into the specified directory.

    The clone_dir must not already exist -- this function creates it.

    The two cases take deliberately different code paths:

    No ``branch`` given: a plain ``git clone <url> <dir>``. This resolves
    the remote's default branch natively (in one connection), creates a
    matching *named* local branch, and checks it out -- exactly the state a
    user gets from ``git clone``. The named branch is load-bearing: the
    downstream ``mngr create`` mirror push only pushes ``refs/heads/*`` +
    ``refs/tags/*`` (a detached HEAD leaves ``refs/heads/*`` empty and the
    push fails with "No refs in common and none specified; doing nothing"),
    and the resolved name becomes the agent's source-base branch. Letting
    git resolve the default branch avoids parsing ``ls-remote`` output or
    making a second round trip whose name could disagree with the fetch.

    Explicit ``branch`` (a branch name, tag name, or commit SHA): ``git
    init`` + ``git remote add origin`` + ``git fetch origin <ref>`` + ``git
    checkout --detach FETCH_HEAD``, then the caller renames the detached
    HEAD to a real local branch via :func:`checkout_branch`. We avoid ``git
    clone --branch <ref>`` here because ``--branch`` rejects commit SHAs
    (``fatal: Remote branch <sha> not found in upstream origin``); ``git
    fetch`` accepts a branch, tag, or SHA uniformly. The fetch downloads
    only the requested ref's full ancestry.

    Both paths materialise a checked-out working tree, which is
    load-bearing: callers that overlay a worktree via
    :func:`rsync_worktree_over_clone` need a *checked-out* clone, else the
    rsync'd files land untracked and the subsequent ``checkout_branch``
    aborts with "untracked working tree files would be overwritten by
    checkout".

    We deliberately do NOT shallow-clone (no ``--depth``): this clone is
    the source ``mngr create`` mirror-pushes into the agent container's
    bare repo, and git rejects pushes from a shallow source with "shallow
    update not allowed" (the pushed tip's parent is missing from the pack).

    Raises GitCloneError if any step fails (including when ``branch`` does
    not exist on the remote and is not a reachable commit).
    """
    logger.debug("Cloning {} to {}", _redact_url_credentials(str(git_url)), clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=False)

    # Wrap the caller's on_output so git's per-line stdout/stderr is scrubbed
    # of embedded credentials before being forwarded. Git commonly echoes the
    # full clone URL in error messages (e.g. `fatal: unable to access '...'`),
    # which would otherwise leak tokens from credentialed URLs into logs.
    redacted_on_output = _RedactingOutputCallback(inner=on_output) if on_output is not None else None

    # All steps run under the same child concurrency group so cancellation is
    # uniform; the failure is raised AFTER the `with cg` block to keep
    # GitCloneError from being wrapped in a ConcurrencyExceptionGroup. For the
    # explicit-ref path, `init`/`remote add` are local-only and never fail in
    # healthy environments; `fetch` is the step that can legitimately error
    # (auth, network, ref-not-found).
    cg = _make_child_cg("git-clone", parent_cg)
    failed: tuple[str, str] | None = None
    with cg:
        if branch is None:
            # Plain clone: git resolves the remote's default branch and leaves a
            # named local branch checked out (see docstring for why this matters).
            commands: tuple[list[str], ...] = (["git", "clone", str(git_url), str(clone_dir)],)
        else:
            commands = (
                ["git", "init", "-q"],
                ["git", "remote", "add", "origin", str(git_url)],
                ["git", "fetch", "origin", str(branch)],
                ["git", "checkout", "--detach", "FETCH_HEAD"],
            )
        for command in commands:
            result = cg.run_process_to_completion(
                command=command,
                cwd=clone_dir,
                is_checked_after=False,
                on_output=redacted_on_output,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                failed = (command[1], stderr if stderr else stdout)
                break
    if failed is not None:
        step_name, output = failed
        raise GitCloneError("git {} failed:\n{}".format(step_name, _redact_url_credentials_in_text(output)))


_FULL_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


def checkout_branch(
    repo_dir: Path,
    branch: GitBranch,
    on_output: OutputCallback | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Check out the just-fetched ref as a named local branch.

    Uses ``git checkout -B <local-name> FETCH_HEAD`` -- FETCH_HEAD is the
    pseudo-ref :func:`clone_git_repo`'s fetch just landed on, so this is
    the unambiguous source whether the input was a branch, a tag, or a
    SHA. ``-B`` creates the local branch (rather than leaving HEAD
    detached) so downstream ``mngr.create``'s source-base autodetection
    (``git rev-parse --abbrev-ref HEAD``) returns a real branch name.

    When ``branch`` is a 40-char lowercase hex SHA, the local branch is
    named ``sha-<sha>`` instead of ``<sha>`` to avoid git's "refname is
    ambiguous" warning that fires on any subsequent operation that types
    a 40-hex string. Cosmetic only -- operations work either way.

    Raises GitOperationError if the checkout fails.
    """
    ref = str(branch)
    local_name = f"sha-{ref}" if _FULL_SHA_RE.match(ref) else ref
    logger.debug("Checking out {} as local branch {} in {}", ref, local_name, repo_dir)
    cg = _make_child_cg("git-checkout", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "checkout", "-B", local_name, "FETCH_HEAD"],
            cwd=repo_dir,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise GitOperationError(
            "git checkout failed for ref '{}' (exit code {}):\n{}".format(
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
    """Rsync a worktree's working directory over a fresh clone.

    Thin wrapper around :func:`imbue.mngr.utils.git_utils.rsync_worktree_over_clone`
    that owns the per-call ``rsync-worktree`` child CG. The shared helper
    is also what ``mngr_vps`` uses for its docker-build-context
    assembly, so the two paths can't drift again.
    """
    cg = _make_child_cg("rsync-worktree", parent_cg)
    with cg:
        rsync_worktree_over_clone(worktree_dir, clone_dir, cg=cg, on_output=on_output)


# Constant agent name for every minds-created agent. Minds runs one agent
# per host, so the agent name carries no per-workspace information; the
# workspace is identified by its host name. Kept as a SafeName-typed
# constant so callers can pass it to ``mngr`` without re-validating. The
# bare string lives in ``backend_resolver`` (the lower-level module that
# also needs it, for the recovery flow's system-services lookup).
_DEFAULT_AGENT_NAME: Final[AgentName] = AgentName(SYSTEM_SERVICES_AGENT_NAME)

# imbue_cloud create-path knobs forwarded as ``-b fast_mode=<value>``. ``require``
# adopts an exact-attribute pre-baked pool host (fast); ``prevent`` leases any
# available host and rebuilds it from the FCT Dockerfile (slow).
_FAST_MODE_REQUIRE: Final[str] = "require"
_FAST_MODE_PREVENT: Final[str] = "prevent"

# ``error_class`` of the imbue_cloud provider's ``FastPathUnavailableError``,
# emitted by ``mngr create --format jsonl`` as a structured
# ``{"event": "error", "error_class": ...}`` line when ``fast_mode=require``
# finds no exact-attribute pool match. minds matches on this (not on
# human-formatted error text) to fall back to the slow path. Kept in sync with
# ``imbue.mngr_imbue_cloud.errors.FastPathUnavailableError``.
_FAST_PATH_UNAVAILABLE_ERROR_CLASS: Final[str] = "FastPathUnavailableError"


def _build_mngr_create_command(
    launch_mode: LaunchMode,
    host_name: HostName,
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
    imbue_cloud_fast_mode: str | None = None,
    region: str | None = None,
    latchkey_env: Mapping[str, str] | None = None,
    color: str | None = None,
) -> list[str]:
    """Build the ``mngr create`` command for a freshly-provisioned workspace.

    ``--format jsonl`` is appended so the caller can
    parse the canonical ``AgentId`` out of the trailing ``"event":
    "created"`` line; minds no longer pre-generates an id because for
    imbue_cloud the lease forces it back to the pool host's pre-baked
    id anyway, and pre-generating one led to bugs (e.g. keying gateway
    state under a fictional id).

    DOCKER mode: --template main --template docker (runs in Docker container)
    LIMA mode: --template main --template lima (runs in Lima VM)
    VULTR mode: --template main --template vultr (runs in Docker on a Vultr VPS)
    AWS mode: --new-host on the aws-<region> provider, --template main
        --template aws (runs in a runsc Docker container on an EC2 instance;
        the region-specific provider block is written by minds at startup)
    IMBUE_CLOUD mode: --new-host on the imbue_cloud_<slug> provider (the
        plugin's create_host adopts the pool's pre-baked agent under
        the lease's baked name); ``imbue_cloud_*`` arguments encode the
        lease attributes (--build-arg).

    Every mode creates a separate host, so the agent address uses
    ``system-services@<host_name>`` -- the agent name is constant across
    every minds workspace; the host name (the user's input from the
    create-project form) is the workspace identifier. Only IMBUE_CLOUD
    passes ``--reuse`` (to satisfy the pre-baked services-agent on the
    pool host); the other modes rely on ``--new-host`` for fresh-host
    intent and pass neither ``--reuse`` nor ``--update`` because
    mngr's ``--reuse`` matches on agent name without host scope.

    Secrets (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``) are forwarded by
    the FCT template's own ``pass_(host_)env`` declarations, not by inline
    flags here -- ``run_mngr_create`` populates them in the subprocess env
    when needed and the template-declared forwards pick them up. Keeping the
    forwarding declaration in FCT means the same template works for ``mngr
    create`` invocations from outside minds too.

    ``latchkey_env`` is the latchkey wiring (gateway URL, password, JWT,
    disable-counting flag) computed by
    :func:`imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey`. The
    caller decides whether the agent is tunneled (constant agent-side
    loopback URL) or running on the bare host (live gateway port);
    this function just lifts the entries into ``--host-env`` flags so
    every agent that ever runs on the new host inherits the same
    gateway wiring. Pass ``None`` or an empty dict to opt the host out
    of latchkey wiring.
    """
    match launch_mode:
        case LaunchMode.DOCKER:
            address = f"{_DEFAULT_AGENT_NAME}@{host_name}.docker"
        case LaunchMode.LIMA:
            address = f"{_DEFAULT_AGENT_NAME}@{host_name}.lima"
        case LaunchMode.VULTR:
            address = f"{_DEFAULT_AGENT_NAME}@{host_name}.vultr"
        case LaunchMode.AWS:
            # AWS is region-locked per provider instance (EC2's API is
            # per-region), so minds writes one ``[providers.aws-<region>]``
            # block per configured region at startup and the create address
            # selects the region-specific provider. The region is required.
            if not region:
                raise MngrCommandError("AWS mode requires a region")
            address = f"{_DEFAULT_AGENT_NAME}@{host_name}.aws-{region}"
        case LaunchMode.IMBUE_CLOUD:
            if not imbue_cloud_account:
                raise MngrCommandError("IMBUE_CLOUD mode requires imbue_cloud_account")
            slug = _slugify_account(imbue_cloud_account)
            address = f"{_DEFAULT_AGENT_NAME}@{host_name}.imbue_cloud_{slug}"
        case _ as unreachable:
            assert_never(unreachable)

    # The `/welcome` initial message is now baked into the FCT template's
    # [create_templates.main] section, so we no longer pass `--message` here.
    # ``--format jsonl`` makes mngr emit ``{"event": "created", "agent_id": ..., "host_id": ...}``
    # as the final stdout line; ``run_mngr_create`` parses that to recover
    # the canonical agent id (and the canonical host id, used to swing
    # the latchkey opaque permissions handle onto its canonical path).
    latchkey_host_env_args: list[str] = []
    if latchkey_env:
        for key, value in latchkey_env.items():
            # ``--host-env`` (not ``--env``) so the wiring is written to
            # the host's env file once and every agent on the host
            # inherits the same gateway URL / password / JWT.
            latchkey_host_env_args.extend(["--host-env", f"{key}={value}"])

    color_label_args: list[str] = []
    if color is not None:
        # Pre-normalized by the caller (or the form POST handler) to
        # ``#rrggbb`` lowercase; defended in depth by the same
        # ``normalize_workspace_color`` call on the create-route side.
        color_label_args = ["--label", f"color={color}"]

    mngr_command: list[str] = [
        MNGR_BINARY,
        "create",
        address,
        "--no-connect",
        "--format",
        "jsonl",
        "--label",
        f"workspace={host_name}",
        # Pin the agent's per-workspace branch to the host name. mngr's
        # default for ``--branch`` is ``:mngr/*`` where ``*`` expands to the
        # agent name, but our agent name is the constant ``system-services``
        # -- without this override every workspace would share the same
        # branch ``mngr/system-services``. ``:`` keeps the base branch as
        # ``current`` so we just rename the *new* branch.
        "--branch",
        f":mngr/{host_name}",
        "--label",
        "user_created=true",
        *latchkey_host_env_args,
        "--label",
        "is_primary=true",
        *color_label_args,
    ]

    match launch_mode:
        case LaunchMode.IMBUE_CLOUD:
            # The pool host already has a baked ``system-services`` agent
            # (per ``_BAKED_SERVICES_AGENT_NAME`` in
            # ``mngr_imbue_cloud/cli/admin.py``) which the lease/adopt path
            # in ``ImbueCloudHost.create_agent_state`` will hydrate in
            # place. mngr's core create flow runs an "agent already
            # exists on this host" pre-flight that fires before the
            # adopt path -- without ``--reuse`` it aborts with
            # ``An agent named 'system-services' already exists``.
            # ``--reuse`` tells mngr's pre-flight to expect the existing
            # agent; the adopt path then keeps the baked id intact.
            # ``--update`` is intentionally NOT passed: the adopt path
            # already patches the labels + command in place; running
            # mngr's standard provisioning on top would re-do the file
            # transfer + provisioning round the bake already paid for.
            mngr_command.append("--reuse")
        case _:
            # Non-IMBUE_CLOUD modes pass neither ``--reuse`` nor ``--update``:
            # the create form is "give me a new agent on a new host", and
            # ``--reuse`` matches only on agent name (``system-services``)
            # without scoping to host, so it collides across hosts. The
            # ``--new-host`` flag below already covers fresh-host intent.
            pass

    # Per-mode template + per-mode runtime flags. All modes use
    # ``--template main --template <mode>``; the per-mode template provides
    # the provider-specific knobs (idle_mode, pass_host_env, build_arg, ...)
    # while runtime-only knobs that vary per-invocation (``--new-host``,
    # ``-b lease_attributes``) stay inline.
    match launch_mode:
        case LaunchMode.DOCKER:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "docker"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.LIMA:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "lima"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.VULTR:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "vultr"])
            mngr_command.extend(_remote_host_env_flags())
            # The user always picks a Vultr region in the create form (advanced
            # settings). It is a hard placement requirement: the VPS is created
            # in exactly this region.
            if region:
                mngr_command.extend(["-b", f"--vultr-region={region}"])
        case LaunchMode.AWS:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "aws"])
            mngr_command.extend(_remote_host_env_flags())
            # The create address already selects the ``aws-<region>`` provider
            # (whose block is pinned to this region). Pass the matching
            # ``--aws-region`` build arg too so intent is explicit and the
            # provider's cross-region guard confirms the placement.
            if region:
                mngr_command.extend(["-b", f"--aws-region={region}"])
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
            # ``fast_mode`` selects the imbue_cloud create path: ``require``
            # adopts an exact-attribute pre-baked pool host (fast); ``prevent``
            # leases any available host and rebuilds it from the FCT Dockerfile
            # (slow, but always works). minds tries ``require`` first and falls
            # back to ``prevent`` on FastPathUnavailableError (see
            # ``_run_imbue_cloud_create_with_fallback``).
            if imbue_cloud_fast_mode:
                mngr_command.extend(["-b", f"fast_mode={imbue_cloud_fast_mode}"])
            # ``region`` is the explicit datacenter the user picked in the create
            # form (advanced settings). It is a hard requirement: the lease only
            # adopts/leases a host in this region, and the user gets a clear
            # "no capacity in <region>" error if none is available there.
            if region:
                mngr_command.extend(["-b", f"region={region}"])
        case _ as unreachable:
            assert_never(unreachable)

    return mngr_command


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
    API URLs), independent of the local ``MNGR_HOST_DIR`` (which could
    be ``~/.minds/mngr`` for production or ``~/.minds-<env-name>/mngr``
    for any other activated env). We only propagate ``MNGR_PREFIX`` so
    the inner mngr's tmux/session names match the local ones, avoiding
    confusion when the same name has to refer to the "same" thing on
    both sides.
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
    error_class: str | None = Field(
        default=None,
        description=(
            "Populated when a JSONL ``error`` event is seen on stdout. Carries mngr's exception "
            "class name (e.g. ``FastPathUnavailableError``) so callers can branch on the error "
            "*type* instead of substring-matching human-formatted text."
        ),
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
        if not isinstance(event, dict):
            return
        event_type = event.get("event")
        if event_type == "error":
            error_class_raw = event.get("error_class")
            if isinstance(error_class_raw, str) and error_class_raw:
                self.error_class = error_class_raw
            return
        if event_type != "created":
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
    host_name: HostName,
    on_output: OutputCallback | None = None,
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
    imbue_cloud_fast_mode: str | None = None,
    region: str | None = None,
    anthropic_api_key: str | None = None,
    anthropic_base_url: str | None = None,
    latchkey_env: Mapping[str, str] | None = None,
    color: str | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> tuple[AgentId, HostId]:
    """Create an mngr agent via ``mngr create --format jsonl``.

    The repo's own ``.mngr/settings.toml`` defines agent types, templates,
    environment variables, and all other configuration. ``workspace_dir`` is
    the cwd the subprocess runs in (so ``mngr create`` picks up the local
    repo's ``.mngr/`` settings); IMBUE_CLOUD passes ``None`` because the
    pool host has its own pre-baked ``.mngr/`` and the local repo is
    irrelevant.

    ``anthropic_api_key`` / ``anthropic_base_url`` are placed into the
    subprocess env (not argv) so they don't show up in ``ps`` output; the FCT
    template's own ``pass_(host_)env`` declarations cause mngr to forward them
    onto the host as appropriate.

    Returns ``(canonical_agent_id, canonical_host_id)``. Both canonical
    ids are parsed out of the ``"event": "created"`` JSONL line that
    ``mngr create`` emits as its final stdout record; the host id is
    what minds keys per-host latchkey state (permissions, opaque handle
    symlink target) by.

    Raises ``MngrCommandError`` if the command fails or never emits a
    ``created`` event (e.g. crashed before final-output stage).
    """
    mngr_command = _build_mngr_create_command(
        launch_mode,
        host_name,
        imbue_cloud_account=imbue_cloud_account,
        imbue_cloud_repo_url=imbue_cloud_repo_url,
        imbue_cloud_branch_or_tag=imbue_cloud_branch_or_tag,
        imbue_cloud_fast_mode=imbue_cloud_fast_mode,
        region=region,
        latchkey_env=latchkey_env,
        color=color,
    )

    # Build the subprocess env from the parent's env + any secrets we inject
    # for the matching ``--pass-(host-)env`` flag to forward. Mutating
    # ``os.environ`` directly would leak the user's secrets into the desktop
    # client's other subprocesses, so we keep the override scoped to this
    # invocation.
    subprocess_env: dict[str, str] | None = None
    if anthropic_api_key is not None or anthropic_base_url is not None:
        subprocess_env = dict(os.environ)
        if anthropic_api_key is not None:
            subprocess_env["ANTHROPIC_API_KEY"] = anthropic_api_key
        if anthropic_base_url is not None:
            subprocess_env["ANTHROPIC_BASE_URL"] = anthropic_base_url

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
            ),
            error_class=capture.error_class,
        )

    if capture.canonical_agent_id is None or capture.canonical_host_id is None:
        # Exit-zero without a created event almost certainly means the
        # JSONL output got mangled or some pre-emit error path took over.
        # Fail loudly rather than fall through with a sentinel id.
        raise MngrCommandError(
            "mngr create exited 0 but did not emit a JSONL 'created' event; stdout tail:\n{}".format(
                result.stdout.strip()[-2000:]
            )
        )

    try:
        canonical_host_id = HostId(capture.canonical_host_id)
    except ValueError as e:
        raise MngrCommandError(f"mngr create emitted an invalid host_id {capture.canonical_host_id!r}: {e}") from e

    return capture.canonical_agent_id, canonical_host_id


def run_mngr_aws_prepare(
    region: str,
    on_output: OutputCallback | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Ensure the AWS security group for ``region`` exists before an AWS create.

    Runs ``mngr aws prepare --provider aws-<region> --region <region>``, which is
    read-only-first: when the ``mngr-aws`` security group already exists with the
    required SSH ingress it issues no write call, so this succeeds even with an
    AWS key that only has ``ec2:DescribeSecurityGroups``. It only attempts the
    privileged create/authorize when the group (or a rule) is missing.

    ``AwsProvider.create_host`` refuses to launch an instance when the security
    group is absent (it looks it up read-only), so minds runs this first for the
    chosen region. Failures -- missing credentials, or a missing group the key
    cannot create -- raise ``MngrCommandError`` so the creation flow surfaces a
    clear message on the creating page rather than a deferred opaque create
    failure.
    """
    # AWS is region-locked per provider instance, so a region is required to
    # name the ``aws-<region>`` provider. Fail fast with the same message
    # ``_build_mngr_create_command`` raises so the empty-region case is rejected
    # consistently regardless of which step trips first.
    if not region:
        raise MngrCommandError("AWS mode requires a region")
    provider_name = f"aws-{region}"
    command = [MNGR_BINARY, "aws", "prepare", "--provider", provider_name, "--region", region]
    logger.info("Running: {}", " ".join(command))
    cg = _make_child_cg("mngr-aws-prepare", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise MngrCommandError(
            "mngr aws prepare failed for region {} (exit code {}):\n{}".format(
                region,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


class _MngrCreateAttemptParams(FrozenModel):
    """Per-creation inputs shared across a ``fast_mode`` retry loop.

    Bundles everything ``_attempt_mngr_create`` needs except the ``fast_mode``
    knob, which is the only value that differs between the fast-path and
    slow-path attempts.
    """

    launch_mode: LaunchMode
    workspace_dir: Path | None
    host_name: HostName
    on_output: OutputCallback
    latchkey_env: Mapping[str, str] | None
    account_email: str | None
    repo_source: str | None
    branch_or_tag: str | None
    region: str | None
    anthropic_api_key: str | None
    anthropic_base_url: str | None
    parent_cg: ConcurrencyGroup | None
    color: str | None


def _attempt_mngr_create(fast_mode: str | None, params: _MngrCreateAttemptParams) -> tuple[AgentId, HostId]:
    """Run a single ``mngr create`` attempt for ``create``'s ``fast_mode`` retry loop.

    ``fast_mode`` is the only knob that varies between the fast-path and
    slow-path attempts; the imbue_cloud-only inputs are gated on ``launch_mode``
    exactly as before.
    """
    is_imbue_cloud = params.launch_mode is LaunchMode.IMBUE_CLOUD
    return run_mngr_create(
        launch_mode=params.launch_mode,
        workspace_dir=params.workspace_dir,
        host_name=params.host_name,
        on_output=params.on_output,
        latchkey_env=params.latchkey_env,
        imbue_cloud_account=params.account_email if is_imbue_cloud else None,
        # Pass the form's repository through verbatim (a remote URL in
        # production, a local clone path in dev). The provider canonicalizes it
        # -- resolving a local path to its ``origin`` remote -- so the fast path
        # adopts a pool host only when the request's repo *and* branch genuinely
        # match what was baked. minds must not canonicalize here (it shells out
        # to ``mngr`` and cannot import the plugin).
        imbue_cloud_repo_url=(params.repo_source if is_imbue_cloud and params.repo_source else None),
        imbue_cloud_branch_or_tag=(params.branch_or_tag if is_imbue_cloud and params.branch_or_tag else None),
        imbue_cloud_fast_mode=fast_mode,
        # ``region`` is honored by IMBUE_CLOUD (-b region=), VULTR
        # (-b --vultr-region=), and AWS (-b --aws-region=); the command builder
        # ignores it for DOCKER/LIMA.
        region=(params.region or None),
        anthropic_api_key=params.anthropic_api_key,
        anthropic_base_url=params.anthropic_base_url,
        color=params.color,
        parent_cg=params.parent_cg,
    )


def _log_backup_attempt(agent_id: AgentId, retry_state: RetryCallState) -> None:
    """Debug-log a backup-setup retry, called at the start of each retry attempt.

    The first attempt has no prior outcome and is not logged; subsequent attempts
    log the previous attempt's failure so retries are traceable without spamming.
    """
    outcome = retry_state.outcome
    if outcome is None:
        return
    logger.debug(
        "Backup setup attempt {} for agent {} (previous failed: {}); retrying",
        retry_state.attempt_number,
        agent_id,
        outcome.exception(),
    )


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
    latchkey: Latchkey | None = Field(
        default=None,
        frozen=True,
        description=(
            "Latchkey wrapper that owns the shared ``latchkey gateway`` subprocess. When "
            "provided, agent creation derives the gateway's shared password and a per-host "
            "permissions-override JWT, injecting both into the ``mngr create`` env "
            "(``LATCHKEY_GATEWAY_PASSWORD`` so the agent's ``latchkey`` CLI authenticates, "
            "and ``LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`` so the gateway evaluates the "
            "agent's calls against its own deny-all-by-default ``latchkey_permissions.json`` "
            "instead of the gateway's shared default). ``None`` degrades gracefully: the "
            "agent still gets ``LATCHKEY_GATEWAY=...`` (the URL is useful by itself for "
            "tests / non-password-protected gateways), but no password or JWT injection happens."
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
            "probe the freshly-created agent's system_interface through the plugin's per-subdomain "
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
    system_interface_health_tracker: SystemInterfaceHealthTracker = Field(
        frozen=True,
        description=(
            "Per-process health tracker shared with the ``mngr forward`` ``system_interface_backend_failure`` "
            "envelope consumer and the background system-interface-health probe loop. ``_wait_for_workspace_ready`` "
            "calls ``record_probe_success`` on the probe that breaks out of its readiness loop, which clears "
            "the probe-failure run the container's warmup failures have accumulated. Without this call, "
            "a workspace creation whose ``system-interface`` takes a while to bind ``:8000`` would let the "
            "background probe loop drive the agent to STUCK and jump the chrome to the recovery page right "
            "after the user lands on the workspace."
        ),
    )
    workspace_ready_timeout_seconds: float = Field(
        default=300.0,
        frozen=True,
        description=(
            "Maximum time to wait for the new agent's system_interface to return HTTP 200. "
            "First-boot provisioning (uv sync, npm ci + run build for the system_interface "
            "frontend) regularly takes 90-180s on a fresh VM or Docker host, so the previous "
            "60s default left users on the recovery page while the agent was still finishing "
            "provisioning. The probe is cheap so a generous cap is harmless; we still publish "
            "the redirect anyway if it expires."
        ),
    )
    workspace_ready_poll_interval_seconds: float = Field(
        default=0.5,
        frozen=True,
        description="Sleep between probe attempts when the system_interface is not yet ready.",
    )
    workspace_ready_probe_timeout_seconds: float = Field(
        default=2.0,
        frozen=True,
        description="Per-request timeout for the readiness probe HTTP GET.",
    )
    backup_setup_retry_budget_seconds: float = Field(
        default=300.0,
        frozen=True,
        description=(
            "Total wall-clock budget for retrying backup setup on the detached thread. "
            "The workspace is ready before this thread runs, but a slow host's mngr exec can "
            "still race the agent's reachability; we retry transient failures within this budget "
            "before giving up and notifying the user. Never blocks the create call."
        ),
    )
    backup_setup_retry_wait_seconds: float = Field(
        default=10.0,
        frozen=True,
        description="Wait between backup-setup retry attempts.",
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
    _launch_modes: dict[str, LaunchMode] = PrivateAttr(default_factory=dict)
    _host_names: dict[str, str] = PrivateAttr(default_factory=dict)
    _log_queues: dict[str, queue.Queue[str]] = PrivateAttr(default_factory=dict)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_creation(
        self,
        repo_source: str,
        host_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.DOCKER,
        ai_provider: AIProvider = AIProvider.SUBSCRIPTION,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        anthropic_api_key: str = "",
        on_created: Callable[[AgentId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
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
        # ``host_name`` falls back to a repo-derived name when blank so the
        # API path (``/api/create-agent``) doesn't need to compute it itself.
        # The form path already requires the field. ``HostName(...)`` is
        # invoked downstream in ``_create_agent_background`` so any invalid
        # input fails inside the background thread with an error_message
        # rather than crashing this synchronous entry point.
        effective_name = host_name.strip() if host_name.strip() else extract_repo_name(repo_source)
        effective_branch = branch.strip()

        creation_id = CreationId()

        with self._lock:
            self._statuses[str(creation_id)] = AgentCreationStatus.INITIALIZING
            self._launch_modes[str(creation_id)] = launch_mode
            self._host_names[str(creation_id)] = effective_name
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
                region,
                anthropic_api_key,
                on_created,
                backup_request,
                color,
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
                launch_mode=self._launch_modes.get(cid_str, LaunchMode.DOCKER),
                host_name=self._host_names.get(cid_str, ""),
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
        host_name: str,
        branch: str,
        log_queue: queue.Queue[str],
        launch_mode: LaunchMode,
        ai_provider: AIProvider,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        anthropic_api_key: str = "",
        on_created: Callable[[AgentId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
    ) -> None:
        """Background thread that resolves the repo source and creates an mngr agent.

        For ``ai_provider == IMBUE_CLOUD``, mints a LiteLLM key (via the
        plugin CLI) and forwards ``ANTHROPIC_API_KEY``/``ANTHROPIC_BASE_URL``
        onto the host via the subprocess env + matching
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
                # Worker thread takes over from the initial ``INITIALIZING``
                # status that ``start_creation`` set; cloning is the first
                # real action. The caption rendered for this status is
                # launch-mode-aware via ``_STATUS_TEXT_IMBUE_CLOUD``.
                with self._lock:
                    self._statuses[cid_str] = AgentCreationStatus.CLONING_REPO

                if _is_local_path(repo_source):
                    resolved_path = Path(os.path.expanduser(repo_source)).resolve()
                    if not resolved_path.is_dir():
                        raise MngrCommandError("Local path does not exist: {}".format(resolved_path))

                    if _is_git_worktree(resolved_path):
                        # Worktrees have a .git file pointing to the parent repo's
                        # .git/worktrees/ dir, which breaks when copied into Docker.
                        # Clone locally to get a standalone repo.
                        #
                        # Full clone (no --depth=1): a shallow clone only pulls
                        # the default branch (e.g. main) and not the user's
                        # target branch (e.g. pilot), so the subsequent
                        # `git checkout <branch>` fails with `pathspec did not
                        # match`. mngr's downstream mirror push into the agent
                        # container's bare receiver also rejects shallow source
                        # packs with "shallow update not allowed". Cloning
                        # deeply avoids both. Local file:// clones are cheap.
                        # Use a stable path based on repo name so Docker layer caching works.
                        log_queue.put("[minds] Cloning local worktree: {}".format(resolved_path))
                        repo_name = extract_repo_name(repo_source)
                        clone_target = Path(tempfile.gettempdir()) / "minds-clone-{}".format(repo_name)
                        if clone_target.exists():
                            shutil.rmtree(clone_target)
                        file_url = GitUrl("file://{}".format(resolved_path))
                        # Pass the branch through (like the remote-URL case
                        # below) so that when one is requested the clone takes
                        # the fetch-into-FETCH_HEAD path that the subsequent
                        # ``checkout_branch`` depends on. With no branch, the
                        # plain ``git clone`` lands on the worktree's own branch
                        # and ``checkout_branch`` is skipped.
                        clone_git_repo(
                            file_url,
                            clone_target,
                            on_output=emit_log,
                            branch=GitBranch(branch) if branch else None,
                            parent_cg=self.root_concurrency_group,
                        )
                        # Rsync the worktree's working directory over so that
                        # uncommitted changes (e.g. a locally-rsynced
                        # vendor/mngr/) are included in the Docker build context.
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
                    # Clone only the requested branch (non-shallow) when one is
                    # given: cheaper than a full clone, yet keeps the complete
                    # ancestry that the downstream mirror-push into the agent
                    # container requires (a shallow clone would be rejected with
                    # "shallow update not allowed"). Every launch mode reaches
                    # mngr create's git-mirror push (a cloned-repo source + a
                    # new host always resolves to TransferMode.GIT_MIRROR), so a
                    # shallow clone is never safe here regardless of mode. The
                    # checkout below is then a no-op for this path, but still
                    # does the work when the source is a pre-existing local
                    # directory.
                    clone_git_repo(
                        GitUrl(repo_source),
                        clone_target,
                        on_output=emit_log,
                        branch=GitBranch(branch) if branch else None,
                        parent_cg=self.root_concurrency_group,
                    )
                    workspace_dir = clone_target

                if branch:
                    with self._lock:
                        self._statuses[cid_str] = AgentCreationStatus.CHECKING_OUT_BRANCH
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
                        with self._lock:
                            self._statuses[cid_str] = AgentCreationStatus.PROVISIONING_AI
                        log_queue.put(f"[minds] Minting LiteLLM virtual key for account {account_email}...")
                        try:
                            key_material: LiteLLMKeyMaterial = self.imbue_cloud_cli.create_litellm_key(
                                account=account_email,
                                alias=None,
                                max_budget=100.0,
                                budget_duration="1d",
                                metadata={"host_name": host_name},
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
                    self._statuses[cid_str] = AgentCreationStatus.CREATING_WORKSPACE

                # Pre-create the shared latchkey gateway password and a
                # per-host permissions-override JWT before invoking
                # ``mngr create``. The JWT references an *opaque*
                # UUID-named permissions handle that we materialize
                # here with a deny-all baseline; after ``mngr create``
                # returns the canonical host id, ``finalize_host_permissions``
                # replaces that handle with a symlink to the canonical
                # ``permissions_path_for_host`` location. The env vars are
                # injected into the ``mngr create`` env so they are present
                # from the start, avoiding any post-create re-provisioning
                # step. Every launch mode is ``is_tunneled=True`` since the
                # only on-host launch mode (DEV) was removed -- all remaining
                # modes reach the gateway via the reverse tunnel
                # ``LatchkeyDiscoveryHandler`` sets up post-discovery.
                #
                # ``prepare_agent_latchkey`` raises on infrastructure
                # failures (latchkey CLI broken, on-disk write failed,
                # etc.). Minds tolerates those by falling back to an
                # empty setup so the agent still comes up -- it just
                # won't authenticate to a password-protected gateway and
                # won't have its own permissions file. The user can
                # recover by fixing the latchkey installation and
                # re-creating the agent.
                latchkey_setup = self._prepare_latchkey_or_warn(log_queue)

                # AWS hosts need the region's security group to exist before
                # ``mngr create`` (the provider looks it up read-only and
                # refuses to launch without it). prepare is read-only-first, so
                # this is a no-op describe when the region is already prepared.
                if launch_mode is LaunchMode.AWS:
                    log_queue.put(f"[minds] Ensuring AWS security group is ready in {region}...")
                    run_mngr_aws_prepare(region, on_output=emit_log, parent_cg=self.root_concurrency_group)

                parsed_host = HostName(host_name)
                log_queue.put("[minds] Creating workspace '{}' (mode: {})...".format(host_name, launch_mode.value))

                # ``fast_mode`` is the only knob that varies between the fast-
                # path and slow-path attempts; bundle the rest of the per-
                # creation inputs so each attempt takes just it.
                attempt_params = _MngrCreateAttemptParams(
                    launch_mode=launch_mode,
                    workspace_dir=workspace_dir,
                    host_name=parsed_host,
                    on_output=emit_log,
                    latchkey_env=latchkey_setup.env,
                    account_email=account_email,
                    repo_source=repo_source,
                    branch_or_tag=branch_or_tag,
                    region=region,
                    anthropic_api_key=effective_anthropic_api_key,
                    anthropic_base_url=effective_anthropic_base_url,
                    parent_cg=self.root_concurrency_group,
                    color=color,
                )

                if launch_mode is LaunchMode.IMBUE_CLOUD:
                    canonical_id, canonical_host_id = self._create_imbue_cloud_with_fallback(attempt_params, log_queue)
                else:
                    canonical_id, canonical_host_id = _attempt_mngr_create(None, attempt_params)

                # Now that we know the canonical host id, point the
                # opaque permissions handle (which the JWT references)
                # at the canonical host-keyed permissions file. After
                # this, ``LatchkeyPermissionGrantHandler`` can write to
                # the canonical path and the gateway will see the
                # changes via the symlink. Keying by host (not agent)
                # matches the ``--host-env`` injection above: every
                # agent on the host shares the same gateway wiring and
                # the same permissions file.
                #
                # We downgrade ``LatchkeyStoreError`` here to a warning
                # rather than failing agent creation: the gateway still
                # has the deny-all baseline at the opaque path (the JWT
                # already points there), so the agent comes up working.
                # If the link is never established, the first permission
                # request the agent files is repaired on the fly by
                # ``recover_missing_host_permissions`` (see
                # ``_StreamedPermissionRequestHandler`` in ``cli/run.py``),
                # which swings the opaque handle to the canonical path so
                # later UI-driven grants take effect without a re-create.
                if self.latchkey is not None:
                    try:
                        finalize_host_permissions(
                            self.latchkey,
                            latchkey_setup.opaque_permissions_path,
                            canonical_host_id,
                        )
                    except LatchkeyStoreError as link_error:
                        logger.warning(
                            "Failed to link latchkey permissions handle for host {}: {}",
                            canonical_host_id,
                            link_error,
                        )
                        log_queue.put(
                            "[minds] Warning: could not link latchkey permissions handle to "
                            f"canonical path for host {canonical_host_id}; this will be repaired "
                            f"automatically the first time the agent requests a permission. Reason: {link_error}"
                        )

                log_queue.put("[minds] Agent created successfully.")

                # Wait for the agent's system_interface to actually answer 200
                # through the plugin before publishing the redirect. Without
                # this poll, the user gets dropped on a hard error page (404
                # /503) for the few seconds between ``mngr create`` returning
                # and the system_interface inside the agent finishing
                # startup. The probe is best-effort: if it times out, we
                # publish anyway so the user at least lands on the retry
                # page rather than spinning forever (PR 1471 part 1).
                with self._lock:
                    self._statuses[cid_str] = AgentCreationStatus.WAITING_FOR_READY
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

                # Configure restic backups asynchronously on a detached
                # thread (mirrors the Cloudflare tunnel-token path): bucket
                # creation + injection is a multi-second round-trip we don't
                # want to block the redirect on, and a failure here is
                # non-fatal to the already-created workspace. Skipped (no
                # thread spawned) for CONFIGURE_LATER.
                if backup_request is not None and backup_request.backup_provider is not BackupProvider.CONFIGURE_LATER:
                    self.root_concurrency_group.start_new_thread(
                        target=self._provision_backups,
                        kwargs={
                            "agent_id": canonical_id,
                            "host_id": str(canonical_host_id),
                            "backup_request": backup_request,
                        },
                        name=f"backup-setup-{canonical_id}",
                        # is_checked=False so a failing backup task does not
                        # poison the root CG; failures are surfaced via
                        # notification + loguru from within _provision_backups.
                        is_checked=False,
                    )

        except (GitCloneError, GitOperationError, MngrCommandError, ImbueCloudCliError, ValueError, OSError) as e:
            logger.opt(exception=e).error("Failed to create agent for creation {}", creation_id)
            log_queue.put("[minds] ERROR: {}".format(e))
            with self._lock:
                self._statuses[cid_str] = AgentCreationStatus.FAILED
                self._errors[cid_str] = str(e)
        finally:
            log_queue.put(LOG_SENTINEL)

    def _create_imbue_cloud_with_fallback(
        self,
        attempt_params: _MngrCreateAttemptParams,
        log_queue: queue.Queue[str],
    ) -> tuple[AgentId, HostId]:
        """Try the fast (adopt) path, then fall back to the slow (rebuild) path.

        The first attempt requests ``fast_mode=require`` -- the imbue_cloud
        provider adopts a pre-baked pool host whose attributes exactly match.
        If none is available the provider raises ``FastPathUnavailableError``,
        which ``mngr create --format jsonl`` surfaces as a structured
        ``{"event": "error", "error_class": "FastPathUnavailableError"}`` line;
        minds matches on that ``error_class`` and retries with
        ``fast_mode=prevent``, which leases any available host and rebuilds it
        from the FCT Dockerfile (full client-side setup). Any other failure
        (including a genuinely empty pool) propagates unchanged.
        """
        log_queue.put("[minds] Trying fast path (adopt a matching pre-baked pool host)...")
        try:
            return _attempt_mngr_create(_FAST_MODE_REQUIRE, attempt_params)
        except MngrCommandError as exc:
            if exc.error_class != _FAST_PATH_UNAVAILABLE_ERROR_CLASS:
                raise
            logger.info("imbue_cloud fast path unavailable; retrying with the slow path (full rebuild)")
            log_queue.put(
                "[minds] No matching pre-baked pool host; falling back to slow path (leasing any host "
                "and rebuilding it). This is slower but always works when the pool has free hosts..."
            )
            return _attempt_mngr_create(_FAST_MODE_PREVENT, attempt_params)

    def _prepare_latchkey_or_warn(
        self,
        log_queue: queue.Queue[str],
    ) -> AgentLatchkeySetup:
        """Run :func:`prepare_agent_latchkey` and downgrade its errors to warnings.

        The plugin raises on infrastructure failures so the caller can
        decide. Minds's policy is to fall back to an empty setup -- the
        agent still comes up without latchkey wiring, and the user can
        fix the latchkey installation and re-create the agent.
        """
        try:
            return prepare_agent_latchkey(self.latchkey, is_tunneled=True)
        except LatchkeyError as e:
            logger.warning("Failed to prepare latchkey wiring: {}", e)
            log_queue.put("[minds] Warning: latchkey wiring skipped: {}".format(e))
            return AgentLatchkeySetup(env={}, opaque_permissions_path=None)
        except LatchkeyStoreError as e:
            logger.warning("Failed to materialize latchkey permissions handle: {}", e)
            log_queue.put("[minds] Warning: latchkey wiring skipped: {}".format(e))
            return AgentLatchkeySetup(env={}, opaque_permissions_path=None)

    def _provision_backups(
        self,
        *,
        agent_id: AgentId,
        host_id: str,
        backup_request: BackupSetupRequest,
    ) -> None:
        """Detached-thread entry point: configure restic backups for the new host.

        ``configure_backups_for_host`` is idempotent, so we retry it within a
        bounded wall-clock budget: by the time this thread runs the workspace
        readiness probe has already passed, but a slow host's ``mngr exec`` can
        still race the agent's reachability for a while after that. Transient
        failures are retried quietly (debug-logged per attempt); only if the
        whole budget is exhausted do we surface an OS notification. Either way
        this is non-fatal to the already-created workspace -- the user can
        configure backups later -- and it never blocks the create call.
        """

        try:
            for attempt in Retrying(
                retry=retry_if_exception_type((BackupProvisioningError, ImbueCloudCliError)),
                stop=stop_after_delay(self.backup_setup_retry_budget_seconds),
                wait=wait_fixed(self.backup_setup_retry_wait_seconds),
                reraise=True,
            ):
                with attempt:
                    _log_backup_attempt(agent_id, attempt.retry_state)
                    configure_backups_for_host(
                        agent_id=agent_id,
                        host_id=host_id,
                        request=backup_request,
                        imbue_cloud_cli=self.imbue_cloud_cli,
                        paths=self.paths,
                        parent_cg=self.root_concurrency_group,
                    )
        except (BackupProvisioningError, ImbueCloudCliError) as exc:
            logger.opt(exception=exc).warning(
                "Failed to configure backups for agent {} after {:.0f}s of retries",
                agent_id,
                self.backup_setup_retry_budget_seconds,
            )
            self.notification_dispatcher.dispatch(
                NotificationRequest(
                    title="Backup setup failed",
                    message=(
                        f"Couldn't configure backups for '{str(agent_id)[:8]}'. "
                        f"The workspace is running; backups are not yet set up. Error: {exc}"
                    ),
                    urgency=NotificationUrgency.NORMAL,
                ),
                agent_display_name=str(agent_id)[:8],
            )

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
        """Poll the agent's system_interface through the plugin until it responds 200.

        Probes the plugin on loopback (with the agent's ``agent-<hex>.localhost``
        vhost in the ``Host`` header) and the preauth cookie set, treating any
        200 as ready. Other status codes (typically
        503 from the plugin's auto-refresh page when the system_interface
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

        deadline = time.monotonic() + self.workspace_ready_timeout_seconds
        log_queue.put("[minds] Waiting for system interface to be ready...")
        last_status: int | None = None
        attempt = 0
        with make_workspace_probe_client(
            preauth_cookie=self.mngr_forward_preauth_cookie,
            probe_timeout_seconds=self.workspace_ready_probe_timeout_seconds,
        ) as probe_client:
            while time.monotonic() < deadline:
                attempt += 1
                status = probe_workspace_through_plugin(
                    mngr_forward_port=self.mngr_forward_port,
                    preauth_cookie=self.mngr_forward_preauth_cookie,
                    agent_id=agent_id,
                    probe_timeout_seconds=self.workspace_ready_probe_timeout_seconds,
                    client=probe_client,
                )
                if status is not None:
                    last_status = status
                    if status == 200:
                        logger.debug("Workspace ready for {} after {} probe(s)", agent_id, attempt)
                        log_queue.put("[minds] System interface is ready.")
                        # Propagate the success into the shared health tracker,
                        # clearing the suspect flag and probe-failure run that
                        # the warmup failures enrolled, so the chrome does not
                        # jump to the recovery page right after the user lands on
                        # their freshly-created workspace. (See the tracker's
                        # ``system_interface_health_tracker`` field docstring.)
                        # Idempotent if the tracker has no record for this agent.
                        self.system_interface_health_tracker.record_probe_success(agent_id)
                        return
                threading.Event().wait(timeout=self.workspace_ready_poll_interval_seconds)
        logger.warning(
            "Workspace readiness probe for {} timed out after {:.0f}s (last status={}); publishing redirect anyway",
            agent_id,
            self.workspace_ready_timeout_seconds,
            last_status,
        )
        log_queue.put(
            "[minds] Warning: workspace did not become ready within "
            f"{self.workspace_ready_timeout_seconds:.0f}s; you may see a retry page on first load."
        )
