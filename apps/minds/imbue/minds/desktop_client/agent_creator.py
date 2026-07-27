"""Agent create attempts for the desktop client.

Creates mngr agents from git repositories or local directories. The repo's
own ``.mngr/settings.toml`` drives all configuration -- no minds.toml,
vendoring, or parent tracking.

Agent create attempts run in background threads so the server remains responsive.
Callers can poll create attempt status via get_create_attempt_info() or stream logs
via get_log_sink().
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SkipValidation
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import retry_if_not_exception_type
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
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.labeled_hosts import ListedHost
from imbue.minds.desktop_client.labeled_hosts import WORKSPACE_ID_LABELED_PROVIDER_NAMES
from imbue.minds.desktop_client.labeled_hosts import find_host_by_workspace_id_label
from imbue.minds.desktop_client.labeled_hosts import list_provider_hosts
from imbue.minds.desktop_client.lima_image_prefetch import LimaImageCreateGate
from imbue.minds.desktop_client.lima_image_prefetch import prebaked_image_mngr_setting_args
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.pending_create_attempts import FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.pending_create_attempts import WORKSPACE_ID_HOST_LABEL
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import PendingCreateAttemptStoreError
from imbue.minds.errors import WorkspaceNameInUseError
from imbue.minds.lima_image.primitives import get_current_image_arch
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.minds.utils.secret_redaction import redact_secret_env_assignments
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.utils.git_utils import rsync_worktree_over_clone
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import SECRET_LATCHKEY_ENV_VAR_NAMES
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

# Scheme of the `mngr forward` proxy origin. minds always runs the proxy with
# `--use-http2`, so it terminates TLS and the probe/redirect URLs the Python
# side builds are always `https`.
_MNGR_FORWARD_SCHEME: Final[str] = "https"


def make_workspace_probe_client(preauth_cookie: str, probe_timeout_seconds: float) -> httpx.Client:
    """Construct a reusable httpx.Client preconfigured for workspace probes.

    Callers that probe in a tight poll loop should construct one of these and
    pass it to ``probe_workspace_through_plugin`` on each iteration, instead
    of letting the helper construct a one-shot client per call.

    The proxy serves TLS (HTTP/2), so cert verification is disabled: these
    probes dial ``127.0.0.1`` with a ``Host: agent-<hex>.localhost`` header, so
    hostname verification could never pass, and the cert is a self-signed
    ephemeral one the probe is not positioned to validate anyway. Loopback-only.
    """
    return httpx.Client(
        timeout=probe_timeout_seconds,
        follow_redirects=False,
        cookies={_MNGR_FORWARD_SESSION_COOKIE_NAME: preauth_cookie},
        verify=False,
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
    (create attempt flow) and the system-interface-health tracker's background
    probe loop so both paths agree on what "ready" means.

    Pass a pre-constructed ``client`` (via ``make_workspace_probe_client``)
    to reuse the connection pool across a tight poll loop. When omitted, a
    one-shot client is constructed for this single probe -- fine for
    one-off / sporadic callers but wasteful in a loop.
    """
    probe_url = f"{_MNGR_FORWARD_SCHEME}://127.0.0.1:{mngr_forward_port}{_WORKSPACE_PROBE_PATH}"
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

# Cap on the replayable in-memory create attempt log. Re-entering a creating row
# replays this buffer before tailing live lines; older lines beyond the cap
# are dropped, which a replay surfaces via ``CreateAttemptLogChunk.is_truncated``.
CREATE_ATTEMPT_LOG_REPLAY_MAX_LINES: Final[int] = 10_000


def _new_create_attempt_log_buffer() -> deque[str]:
    return deque(maxlen=CREATE_ATTEMPT_LOG_REPLAY_MAX_LINES)


class CreateAttemptLogChunk(FrozenModel):
    """One reader's view of a create attempt log: the lines at and after its index."""

    lines: tuple[str, ...] = Field(description="Log lines from the requested index onward (possibly empty)")
    next_index: int = Field(description="The index to pass to the next read (past the returned lines)")
    is_done: bool = Field(description="Whether the create attempt has ended (no further lines will ever arrive)")
    is_truncated: bool = Field(
        default=False,
        description="Whether lines between the requested index and the returned ones were dropped by the buffer cap",
    )


class CreateAttemptLogSink(MutableModel):
    """Per-create-attempt replayable log buffer shared by the producer and any number of SSE readers.

    Lines are stored in a bounded in-memory buffer (the last
    ``CREATE_ATTEMPT_LOG_REPLAY_MAX_LINES``) and read by absolute line index, so a
    reader attaching mid-create-attempt -- or re-entering the creating page later --
    replays the retained history from index 0 and then tails live lines. The
    buffer also backs the FAILED pending-create-attempt record's log-tail snapshot
    (``tail_lines``). The buffer is lost on app restart; a restart makes the
    create attempt interrupted anyway, and interrupted/failed rows read the
    persisted record's tail instead.
    """

    buffered_lines: deque[str] = Field(
        default_factory=_new_create_attempt_log_buffer,
        frozen=True,
        description="Bounded buffer of every line put (the log sentinel excluded)",
    )
    appended_line_count: int = Field(
        default=0, description="Total lines ever appended; the buffer holds the most recent of them"
    )
    is_done: bool = Field(default=False, description="Whether the create attempt ended (set by the log sentinel)")
    state_condition: SkipValidation[threading.Condition] = Field(
        default_factory=threading.Condition,
        frozen=True,
        description="Guards the buffer; readers wait on it and every put notifies it",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def put(self, line: str) -> None:
        with self.state_condition:
            if line == LOG_SENTINEL:
                self.is_done = True
            else:
                self.buffered_lines.append(line)
                self.appended_line_count = self.appended_line_count + 1
            self.state_condition.notify_all()

    def _read_available_locked(self, from_index: int) -> CreateAttemptLogChunk:
        first_index = self.appended_line_count - len(self.buffered_lines)
        start = max(from_index - first_index, 0)
        lines = tuple(self.buffered_lines)[start:] if start < len(self.buffered_lines) else ()
        return CreateAttemptLogChunk(
            lines=lines,
            next_index=self.appended_line_count,
            is_done=self.is_done,
            is_truncated=from_index < first_index,
        )

    def read_chunk(self, from_index: int, timeout_seconds: float) -> CreateAttemptLogChunk:
        """Return the log lines at/after ``from_index``; ``from_index=0`` replays the retained history.

        Blocks up to ``timeout_seconds`` when no new lines are available yet
        and the create attempt is still running, so a streaming reader can poll
        without spinning.
        """
        with self.state_condition:
            chunk = self._read_available_locked(from_index)
            if chunk.lines or chunk.is_done:
                return chunk
            self.state_condition.wait(timeout=timeout_seconds)
            return self._read_available_locked(from_index)

    def tail_lines(self, max_line_count: int) -> tuple[str, ...]:
        """The most recent ``max_line_count`` buffered lines (the FAILED-record snapshot)."""
        # Guard the slice: ``[-0:]`` would return the WHOLE buffer, not nothing.
        if max_line_count <= 0:
            return ()
        with self.state_condition:
            buffered = tuple(self.buffered_lines)
        return buffered[-max_line_count:]


def make_log_callback(log_sink: CreateAttemptLogSink) -> OutputCallback:
    """Create an output callback that puts lines into a create attempt log sink."""
    return lambda line, is_stdout: logger.info(line.rstrip("\n")) or log_sink.put(line.rstrip("\n"))


class AgentCreateAttemptStatus(UpperCaseStrEnum):
    """Status of a background agent create attempt.

    The non-terminal values correspond to the ordered phases the worker
    thread walks through; ``_stream_create_attempt_logs`` polls the current
    status and emits a SSE event each time it changes so the UI spinner
    caption stays in sync with what the backend is actually doing.
    Conditional phases (``CHECKING_OUT_BRANCH`` only if a branch was
    given) are skipped when they don't apply -- the status simply jumps to
    the next applicable phase.
    """

    INITIALIZING = auto()
    CLONING_REPO = auto()
    CHECKING_OUT_BRANCH = auto()
    CREATING_WORKSPACE = auto()
    WAITING_FOR_READY = auto()
    DONE = auto()
    FAILED = auto()


class CreateAttemptErrorKind(UpperCaseStrEnum):
    """Machine-readable classification of a create attempt failure.

    Carried alongside the human-readable ``error`` message so the creating
    page can gate extra static guidance on the failure *type* instead of
    substring-matching the message client-side. Only failure kinds that
    change what the UI shows get a value here; unclassified failures carry
    no kind and the UI shows just the error message.
    """

    # The clone of a github.com workspace source failed. By far the most
    # common cause: the repo is private (or does not exist -- GitHub
    # deliberately answers both the same way, to avoid leaking which private
    # repos exist) and none of this machine's git credentials can see it, so
    # the creating page shows GitHub sign-in guidance alongside the raw error.
    # The clone mechanism is git, but the problem we surface is GitHub access.
    GITHUB_AUTH_REQUIRED = auto()

    # The clone of a NON-github remote git source (a URL on another host, or an
    # ssh remote) failed -- same likely cause (private/nonexistent, no usable
    # credentials on this machine) and same guidance, minus the GitHub-CLI
    # advice, which only fits github.com. The creating page shows generic
    # git-credentials guidance for this kind.
    GIT_AUTH_REQUIRED = auto()


class AgentCreateAttemptInfo(FrozenModel):
    """Snapshot of agent create attempt state, returned to callers for status polling.

    The agent create attempt flow is keyed by ``create_attempt_id`` (a minds-internal
    handle returned synchronously from :py:meth:`AgentCreator.start_create_attempt`)
    because the canonical ``AgentId`` is only known *after* the inner
    ``mngr create`` returns -- for imbue_cloud agents the id is dictated
    by the leased pool host's pre-baked agent, not by minds. ``agent_id``
    is therefore ``None`` until the inner ``mngr create`` emits its
    ``"event": "created"`` JSONL line; consumers that need to redirect
    to ``/goto/<agent_id>/`` should poll ``redirect_url`` instead, which
    is populated atomically with the ``DONE`` status.
    """

    create_attempt_id: CreateAttemptId = Field(description="Minds-internal handle for this in-flight create attempt")
    agent_id: AgentId | None = Field(
        default=None,
        description="Canonical mngr agent id; populated once ``mngr create`` returns, ``None`` while in-flight",
    )
    status: AgentCreateAttemptStatus = Field(description="Current create attempt status")
    launch_mode: LaunchMode = Field(
        description=(
            "Launch mode for this create attempt. Carried alongside status so consumers can resolve "
            "mode-aware status captions without a separate lookup."
        ),
    )
    host_name: str = Field(
        default="",
        description=(
            "Resolved workspace/host name for this create attempt (the form's Name field, or a "
            "repo-derived fallback). Carried alongside status as create attempt metadata."
        ),
    )
    provider_instance_name: str = Field(
        default="",
        description=(
            "Provider instance this create attempt targets (the host-name uniqueness scope), or empty when "
            "it could not be resolved. Lets the create-attempt-rows derivation label rows per provider."
        ),
    )
    redirect_url: str | None = Field(default=None, description="URL to redirect to when the create attempt is done")
    error: str | None = Field(default=None, description="Error message, set when status is FAILED")
    error_kind: CreateAttemptErrorKind | None = Field(
        default=None,
        description=(
            "Machine-readable classification of the failure, set alongside ``error`` when the "
            "failure is recognized (see ``classify_create_attempt_error``); ``None`` otherwise"
        ),
    )


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


def _is_github_https_url(repo_source: str) -> bool:
    """Check if a repo source is an http(s) URL on github.com.

    Gates the private-repo failure classification (and the sign-in guidance
    the creating page shows for it, which recommends the GitHub CLI) to
    sources where that guidance is actually correct.
    """
    parts = urlsplit(repo_source)
    if parts.scheme not in ("http", "https"):
        return False
    return parts.hostname in ("github.com", "www.github.com")


def _is_remote_git_source(repo_source: str) -> bool:
    """Check if a repo source is a REMOTE git source (a URL or ssh remote).

    True for any ``scheme://`` URL (https/http/ssh/git) and for scp-style ssh
    remotes (``user@host:path``). False for local paths and for bare strings
    that are neither -- so a clone failure on a local path (not an access
    problem) or on garbage input does not get the "you need access" guidance.
    """
    if "://" in repo_source:
        return True
    # scp-style ssh remote, e.g. git@gitlab.example.com:group/repo.git. The
    # host part (before the first ':') must contain no '/', which distinguishes
    # it from a local path like ``./a:b``.
    return bool(re.match(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:", repo_source))


def classify_create_attempt_error(repo_source: str, error: Exception) -> CreateAttemptErrorKind | None:
    """Classify a create attempt failure into a ``CreateAttemptErrorKind``, when recognizable.

    Recognizes two cases, both for a failed clone (``GitCloneError``) of a
    REMOTE git source -- the likely cause is the same (private/nonexistent,
    no usable credentials on this machine). Deliberately no matching of git's
    error text (git has no structured error output, and substring matching is
    brittle across git versions and locales): a remote clone that failed at
    all is overwhelmingly an access problem, and the creating page's guidance
    covers it while the raw git error stays visible right above for anything
    rarer.

    - ``https://github.com/...`` -> ``GITHUB_AUTH_REQUIRED`` (guidance names
      the GitHub CLI, which only fits github.com https).
    - any other remote git source (a URL on another host, or an ssh remote)
      -> ``GIT_AUTH_REQUIRED`` (generic git-credentials guidance, no GitHub CLI).

    A local path or unrecognized input returns ``None`` (just the raw error).
    """
    if not isinstance(error, GitCloneError):
        return None
    if _is_github_https_url(repo_source):
        return CreateAttemptErrorKind.GITHUB_AUTH_REQUIRED
    if _is_remote_git_source(repo_source):
        return CreateAttemptErrorKind.GIT_AUTH_REQUIRED
    return None


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


def _git_noninteractive_env() -> dict[str, str]:
    """Environment for the desktop client's git calls: never prompt for credentials.

    Git prompts for a username/password on the controlling terminal when a
    remote needs auth and no credential is available -- but the desktop client
    has no terminal for the user to answer on, and when minds is launched from
    a dev shell the prompt would hang the create attempt thread forever. With
    ``GIT_TERMINAL_PROMPT=0``, cloning a repo this machine lacks credentials
    for fails fast with git's stable "could not read Username ... terminal
    prompts disabled" error instead of hanging. Credential helpers (e.g. the
    macOS keychain) still work as usual -- only interactive terminal
    prompting is disabled.

    Deliberately a small per-file copy of the same one-line helper the default
    workspace template's ``bootstrap.manager`` and ``runtime_backup.runner``
    carry (same name, same body), rather than a shared cross-package import.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


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

    git_env = _git_noninteractive_env()

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
                env=git_env,
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


def checkout_existing_branch(
    repo_dir: Path,
    branch: GitBranch,
    on_output: OutputCallback | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Check out ``branch`` by name in a repo that was NOT just fetched into.

    Used for plain local-directory sources, where :func:`checkout_branch`'s
    ``git checkout -B <branch> FETCH_HEAD`` would be wrong twice over: a fresh
    clone has no FETCH_HEAD at all (the checkout fails with "'FETCH_HEAD' is
    not a commit"), and a *stale* FETCH_HEAD left by an unrelated earlier fetch
    would silently reset the user's branch tip to that old commit. This is the
    user's own checkout, not a scratch clone, so the branch tip must never be
    moved.

    A no-op when the repo is already on ``branch``. Otherwise a plain
    ``git checkout <branch>`` (git's remote-branch DWIM applies).

    Raises GitOperationError if the checkout fails (e.g. no such branch).
    """
    cg = _make_child_cg("git-checkout-existing", parent_cg)
    with cg:
        head_result = cg.run_process_to_completion(
            command=["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            is_checked_after=False,
            on_output=on_output,
        )
        if head_result.returncode == 0 and head_result.stdout.strip() == str(branch):
            logger.debug("Repo {} is already on branch {}; skipping checkout", repo_dir, branch)
            return
        logger.debug("Checking out existing branch {} in {}", branch, repo_dir)
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
# available host and rebuilds it from the DEFAULT_WORKSPACE_TEMPLATE Dockerfile (slow).
_FAST_MODE_REQUIRE: Final[str] = "require"
_FAST_MODE_PREVENT: Final[str] = "prevent"

# ``error_class`` of the imbue_cloud provider's ``FastPathUnavailableError``,
# emitted by ``mngr create --format jsonl`` as a structured
# ``{"event": "error", "error_class": ...}`` line when ``fast_mode=require``
# finds no exact-attribute pool match. minds matches on this (not on
# human-formatted error text) to fall back to the slow path. Kept in sync with
# ``imbue.mngr_imbue_cloud.errors.FastPathUnavailableError``.
_FAST_PATH_UNAVAILABLE_ERROR_CLASS: Final[str] = "FastPathUnavailableError"

# How long a gated Lima create blocks waiting for the prefetched image before giving up
# on it and building the workspace in-VM instead. A cold download of the real image
# measures ~6 minutes, so this leaves generous headroom for a slower link while bounding
# the case where the download is not slow but stuck: past this, building in-VM (~5 min)
# gets the user a workspace sooner than continuing to wait. The download keeps running,
# so the next create still gets the fast path.
_PREBAKED_IMAGE_WAIT_TIMEOUT_SECONDS: Final[float] = 600.0
_PREBAKED_IMAGE_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# Readiness window for a Lima create with no pre-baked image. Such a create
# builds the whole workspace inside the VM (setup_system.sh +
# install_dependencies.sh + build_workspace.sh, serially, at 2 vCPU / 4 GB)
# before supervisord ever starts the system interface, which routinely takes
# 15+ minutes cold -- far beyond the standard ``workspace_ready_timeout_seconds``
# window. Deliberately a hardcoded constant (not a config knob): it only
# applies to the one create shape whose provisioning time is structural.
# Docker creates keep the standard window (their build happens before
# ``mngr create`` returns, so the post-create exposure is small).
_BUILD_IN_VM_LIMA_READY_TIMEOUT_SECONDS: Final[float] = 900.0

# Only log the download's progress once it has moved this much, so a 1s poll does not
# flood the create log with near-identical lines.
_PREBAKED_IMAGE_PROGRESS_LOG_STEP_BYTES: Final[int] = 500 * 1000 * 1000

# Ceilings for the implicit-discard mngr subprocesses that clean up a dead
# same-name create attempt before a fresh create: ``mngr list --hosts`` runs a live
# provider discovery, and ``mngr destroy`` tears down a VM. Both are one-shot
# pre-create work, so the ceilings are generous rather than tight (matching
# the startup reconcile's).
_DEAD_CREATE_ATTEMPT_HOST_LIST_TIMEOUT_SECONDS: Final[float] = 120.0
_DEAD_CREATE_ATTEMPT_HOST_DESTROY_TIMEOUT_SECONDS: Final[float] = 300.0


class _PrebakedImageProgressReporter(MutableModel):
    """Reports how much of the pre-baked image has downloaded into the create log.

    A create blocked on the image would otherwise show nothing at all: desync draws its
    progress bar only on a tty, so a packaged app sees no output from the download.
    """

    log_line: Callable[[str], None] = Field(frozen=True, description="Sink for one create-log line")
    last_logged_bytes: int = Field(
        default=0, description="Bytes reported by the last line, so a per-second poll does not flood the log"
    )

    def __call__(self, fetched_bytes: int) -> None:
        if fetched_bytes - self.last_logged_bytes < _PREBAKED_IMAGE_PROGRESS_LOG_STEP_BYTES:
            return
        self.last_logged_bytes = fetched_bytes
        self.log_line(f"[minds] Downloading pre-baked Lima image... {fetched_bytes / 1e9:.1f} GB")


class _PrebakedImageFallbackReporter(MutableModel):
    """Tells the create log why the workspace is being built in-VM rather than from the image."""

    log_line: Callable[[str], None] = Field(frozen=True, description="Sink for one create-log line")

    def __call__(self, reason: str) -> None:
        self.log_line(f"[minds] Building the workspace in the VM (slower): {reason}.")


def provider_instance_name_for_launch(
    launch_mode: LaunchMode,
    imbue_cloud_account: str | None = None,
    region: str | None = None,
    cloud_account: str | None = None,
) -> str:
    """Return the mngr provider-instance name a ``mngr create`` on ``launch_mode`` targets.

    This is the scope within which host names must be unique: the create address
    is ``system-services@<host_name>.<provider-instance>`` and the provider's
    ``create_host`` raises ``HostNameConflictError`` only against existing hosts on
    this same instance. Imbue Cloud is per-account (``imbue_cloud_<slug>``) and AWS
    is per-region (``aws-<region>``); the other backends are single instances.

    Kept as the single source of truth for that mapping so the create command and
    the create form's availability check (which must agree on what "taken" means)
    never drift apart. ``imbue_cloud_account`` is the account *email* (slugified to
    match the provider block minds registers); ``region`` is required for AWS.

    ``cloud_account`` is a bring-your-own-key account's provider block name
    (``byok-<backend>-<slug>``, written by ``bootstrap.set_cloud_account_provider``).
    When set it IS the provider instance name, so it short-circuits the
    per-mode mapping (the block already pins backend + credentials + region).
    """
    if cloud_account:
        return cloud_account
    match launch_mode:
        case LaunchMode.DOCKER:
            return "docker"
        case LaunchMode.LIMA:
            return "lima"
        case LaunchMode.VULTR:
            return "vultr"
        case LaunchMode.AWS:
            # BYOK-only (like GCP/AZURE): the ambient per-region ``aws-<region>``
            # path was removed from minds; the ``cloud_account`` short-circuit
            # above is the only way to resolve an AWS provider instance.
            raise MngrCommandError("AWS mode requires a cloud account")
        case LaunchMode.IMBUE_CLOUD:
            if not imbue_cloud_account:
                raise MngrCommandError("IMBUE_CLOUD mode requires imbue_cloud_account")
            return f"imbue_cloud_{_slugify_account(imbue_cloud_account)}"
        case LaunchMode.MODAL:
            # Single instance: the ``modal`` provider talks to Modal with the local
            # token (``modal token new``).
            return "modal"
        case LaunchMode.GCP | LaunchMode.AZURE:
            # GCP / Azure have no ambient provider instances in minds -- they are
            # reachable only through a bring-your-own-key account block, which the
            # ``cloud_account`` short-circuit above already returned.
            raise MngrCommandError(f"{launch_mode.value} mode requires a cloud account")
        case _ as unreachable:
            assert_never(unreachable)


def _build_mngr_create_command(
    launch_mode: LaunchMode,
    host_name: HostName,
    display_name: str = "",
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
    imbue_cloud_fast_mode: str | None = None,
    region: str | None = None,
    cloud_account: str | None = None,
    instance_type: str | None = None,
    latchkey_env: Mapping[str, str] | None = None,
    color: str | None = None,
    docker_runtime: DockerRuntime = DockerRuntime.RUNC,
    original_minds_version: str | None = None,
    original_branch: str | None = None,
    prebaked_lima_image_raw_path: Path | None = None,
    workspace_id_label: str | None = None,
) -> list[str]:
    """Build the ``mngr create`` command for a freshly-provisioned workspace.

    ``--format jsonl`` is appended so the caller can
    parse the canonical ``AgentId`` out of the trailing ``"event":
    "created"`` line; minds no longer pre-generates an id because for
    imbue_cloud the lease forces it back to the pool host's pre-baked
    id anyway, and pre-generating one led to bugs (e.g. keying gateway
    state under a fictional id).

    DOCKER mode: --template main --template docker (runs in a Docker container);
        for ``docker_runtime == RUNSC`` the gVisor overlay is stacked on top
        (--template docker_runsc) so the container runs under runsc. RUNC is the
        docker template's default, so it adds no extra template.
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
    the default workspace template's own ``pass_(host_)env`` declarations, not by inline
    flags here -- ``run_mngr_create`` populates them in the subprocess env
    when needed and the template-declared forwards pick them up. Keeping the
    forwarding declaration in DEFAULT_WORKSPACE_TEMPLATE means the same template works for ``mngr
    create`` invocations from outside minds too.

    ``workspace_id_label`` is the opaque pending-create-attempt id stamped on the
    new HOST as a ``workspace-id`` host label (LIMA and DOCKER only -- the
    callers pass None for the other modes). It is what lets the startup
    reconcile re-associate a host with its pending-create-attempt record after a
    crash or quit mid-create; account/display metadata deliberately stays in
    the local record, never on the host.

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
    # The provider instance the create targets (and thus the scope its host-name
    # uniqueness check runs in) is derived once here so the create address and the
    # form's availability check share a single mapping.
    provider_instance = provider_instance_name_for_launch(
        launch_mode, imbue_cloud_account=imbue_cloud_account, region=region, cloud_account=cloud_account
    )
    address = f"{_DEFAULT_AGENT_NAME}@{host_name}.{provider_instance}"

    # The `/welcome` initial message is now baked into the default workspace template's
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

    # Stamp the minds version the workspace was created at as an immutable
    # label. This is the resolved template ref (a ``minds-v*`` tag in prod,
    # or a branch/``main`` in dev); the workspace's own git history records
    # any later upgrades. Read back by the ``/api/v1/workspaces/<id>/version``
    # route -- the one version fact knowable even for an offline workspace.
    version_label_args: list[str] = []
    if original_minds_version:
        version_label_args = ["--label", f"original_minds_version={original_minds_version}"]

    # Stamp the branch/tag the workspace was created from -- the literal value the
    # user entered in the create form / API ``branch`` field -- as an immutable
    # label, read back by ``/api/v1/workspaces/<id>`` as the ``branch`` field.
    # Absent when the field was left blank (the provider's default branch was
    # used). Distinct from ``original_minds_version`` (the resolved template ref,
    # which for imbue_cloud can be a semver tag rather than a branch).
    branch_label_args: list[str] = []
    if original_branch:
        branch_label_args = ["--label", f"original_branch={original_branch}"]

    mngr_command: list[str] = [
        MNGR_BINARY,
        "create",
        address,
        "--no-connect",
        "--format",
        "jsonl",
        # The workspace's arbitrary human-readable display name lives on the
        # primary (system-services) agent; the host's normalized slug name lives
        # on the host itself. There is no ``workspace`` label. Falls back to the
        # host name when no separate display name is supplied.
        "--label",
        f"workspace_display_name={display_name or host_name}",
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
        *version_label_args,
        *branch_label_args,
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
    # The opaque pending-create-attempt id rides on the host as a ``workspace-id``
    # host label so the startup reconcile can re-attach an orphaned host to its
    # local pending-create-attempt record. Only the local-VM modes get it (the
    # callers pass None otherwise): Modal sandboxes self-expire and imbue_cloud
    # pool hosts have their own reconcile.
    workspace_id_host_label_args: list[str] = []
    if workspace_id_label:
        workspace_id_host_label_args = ["--host-label", f"{WORKSPACE_ID_HOST_LABEL}={workspace_id_label}"]

    match launch_mode:
        case LaunchMode.DOCKER:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "docker"])
            if docker_runtime is DockerRuntime.RUNSC:
                # gVisor overlay: reuses the docker template body and only flips
                # the container runtime to runsc. runc is the docker template's
                # default, so RUNC needs no extra template.
                mngr_command.extend(["--template", "docker_runsc"])
            mngr_command.extend(_remote_host_env_flags())
            mngr_command.extend(workspace_id_host_label_args)
        case LaunchMode.LIMA:
            mngr_command.extend(["--new-host", "--template", "main", "--template", "lima"])
            mngr_command.extend(_remote_host_env_flags())
            mngr_command.extend(workspace_id_host_label_args)
            # Point Lima at the baked raw image via the provider's existing per-arch
            # image-url override, so the VM boots the baked toolchain instead of building it.
            if prebaked_lima_image_raw_path is not None:
                mngr_command.extend(
                    prebaked_image_mngr_setting_args(get_current_image_arch(), prebaked_lima_image_raw_path)
                )
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
            # Per-create machine size (the form's picker); overrides the
            # provider block's default_instance_type when set.
            if instance_type:
                mngr_command.extend(["-b", f"--aws-instance-type={instance_type}"])
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
            # leases any available host and rebuilds it from the DEFAULT_WORKSPACE_TEMPLATE Dockerfile
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
        case LaunchMode.MODAL:
            # Same remote shape as vultr/aws: the ``main`` + ``modal`` templates
            # run the provisioning chain over SSH on the freshly-created sandbox.
            mngr_command.extend(["--new-host", "--template", "main", "--template", "modal"])
            mngr_command.extend(_remote_host_env_flags())
        case LaunchMode.GCP:
            # Same shape as aws; the address already selects the ``byok-gcp-<slug>``
            # account block. GCE is zonal, so the placement flag is ``--gcp-zone``
            # (the form's "region" value for GCP is a zone).
            mngr_command.extend(["--new-host", "--template", "main", "--template", "gcp"])
            mngr_command.extend(_remote_host_env_flags())
            if region:
                mngr_command.extend(["-b", f"--gcp-zone={region}"])
            if instance_type:
                mngr_command.extend(["-b", f"--gcp-machine-type={instance_type}"])
        case LaunchMode.AZURE:
            # Same shape as aws; the address already selects the
            # ``byok-azure-<slug>`` account block.
            mngr_command.extend(["--new-host", "--template", "main", "--template", "azure"])
            mngr_command.extend(_remote_host_env_flags())
            if region:
                mngr_command.extend(["-b", f"--azure-region={region}"])
            if instance_type:
                mngr_command.extend(["-b", f"--azure-vm-size={instance_type}"])
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

    Remote containers always store their mngr state under the workspace
    layout's container-internal path (``/home/user/.mngr``, matching the
    provider blocks' ``host_dir``), independent of the local ``MNGR_HOST_DIR``
    (which could be ``~/.minds/mngr`` for production or
    ``~/.minds-<env-name>/mngr`` for any other activated env). We only
    propagate ``MNGR_PREFIX`` so the inner mngr's tmux/session names match the
    local ones, avoiding confusion when the same name has to refer to the
    "same" thing on both sides.
    """
    return [
        "--host-env",
        f"MNGR_HOST_DIR={WORKSPACE_HOST_DIR}",
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
    display_name: str = "",
    on_output: OutputCallback | None = None,
    imbue_cloud_account: str | None = None,
    imbue_cloud_repo_url: str | None = None,
    imbue_cloud_branch_or_tag: str | None = None,
    imbue_cloud_fast_mode: str | None = None,
    region: str | None = None,
    cloud_account: str | None = None,
    instance_type: str | None = None,
    latchkey_env: Mapping[str, str] | None = None,
    color: str | None = None,
    docker_runtime: DockerRuntime = DockerRuntime.RUNC,
    original_minds_version: str | None = None,
    original_branch: str | None = None,
    prebaked_lima_image_raw_path: Path | None = None,
    workspace_id_label: str | None = None,
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

    No Anthropic credentials are involved at create time: workspace Claude
    auth lives in the env block of the shared CLAUDE_CONFIG_DIR settings.json,
    written by the in-workspace sign-in modal after the workspace boots.

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
        display_name,
        imbue_cloud_account=imbue_cloud_account,
        imbue_cloud_repo_url=imbue_cloud_repo_url,
        imbue_cloud_branch_or_tag=imbue_cloud_branch_or_tag,
        imbue_cloud_fast_mode=imbue_cloud_fast_mode,
        region=region,
        cloud_account=cloud_account,
        instance_type=instance_type,
        latchkey_env=latchkey_env,
        color=color,
        docker_runtime=docker_runtime,
        original_minds_version=original_minds_version,
        original_branch=original_branch,
        prebaked_lima_image_raw_path=prebaked_lima_image_raw_path,
        workspace_id_label=workspace_id_label,
    )

    # The command carries the latchkey gateway password + permissions-override
    # JWT as ``--host-env NAME=VALUE`` flags; mask their values before logging
    # so the persistent logs (uploaded with bug reports) never carry the raw
    # secrets. The subprocess below still receives the unredacted command.
    loggable_command = redact_secret_env_assignments(mngr_command, secret_env_var_names=SECRET_LATCHKEY_ENV_VAR_NAMES)
    loggable_command_str = " ".join(loggable_command)
    logger.info("Running: {}", loggable_command_str)

    capture = _CreateEventCapture(inner_on_output=on_output)
    cg = _make_child_cg("mngr-create", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=mngr_command,
            cwd=workspace_dir,
            is_checked_after=False,
            on_output=capture,
            env=None,
            # Name the reader thread with the redacted command so the gateway
            # password + JWT never reach the JSONL log's ``thread_name`` (nor any
            # ProcessError message); the real command is still what executes.
            name=loggable_command_str,
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
    provider_name: str | None = None,
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
    cannot create -- raise ``MngrCommandError`` so the create attempt flow surfaces a
    clear message on the creating page rather than a deferred opaque create
    failure.
    """
    # AWS is region-locked per provider instance, so a region is required to
    # name the ``aws-<region>`` provider. Fail fast with the same message
    # ``_build_mngr_create_command`` raises so the empty-region case is rejected
    # consistently regardless of which step trips first.
    if not region:
        raise MngrCommandError("AWS mode requires a region")
    # ``provider_name`` overrides the ambient per-region block for
    # bring-your-own-key accounts (``byok-aws-<slug>``), whose block carries the
    # pasted credentials prepare should authenticate with.
    if provider_name is None:
        provider_name = f"aws-{region}"
    _run_mngr_prepare_command(
        [MNGR_BINARY, "aws", "prepare", "--provider", provider_name, "--region", region],
        f"aws prepare for region {region}",
        on_output,
        parent_cg,
    )


def run_mngr_provider_prepare(
    backend: str,
    provider_name: str,
    on_output: OutputCallback | None = None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Run ``mngr <backend> prepare --provider <provider_name>`` (gcp / azure).

    Unlike the AWS path there is no ``--region`` flag: the bring-your-own-key
    account block named by ``provider_name`` already pins the placement
    (``default_zone`` / ``default_region``), the credentials, and the project /
    subscription, and prepare reads all of them from the resolved provider
    config. Idempotent like the AWS prepare; failures raise ``MngrCommandError``.
    """
    _run_mngr_prepare_command(
        [MNGR_BINARY, backend, "prepare", "--provider", provider_name],
        f"{backend} prepare for provider {provider_name}",
        on_output,
        parent_cg,
    )


def _run_mngr_prepare_command(
    command: list[str],
    description: str,
    on_output: OutputCallback | None,
    parent_cg: ConcurrencyGroup | None,
) -> None:
    """Shared runner for the per-provider ``mngr <backend> prepare`` subprocess."""
    logger.info("Running: {}", " ".join(command))
    cg = _make_child_cg("mngr-provider-prepare", parent_cg)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
            on_output=on_output,
        )
    if result.returncode != 0:
        raise MngrCommandError(
            "mngr {} failed (exit code {}):\n{}".format(
                description,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


class _MngrCreateAttemptParams(FrozenModel):
    """Per-create-attempt inputs shared across a ``fast_mode`` retry loop.

    Bundles everything ``_attempt_mngr_create`` needs except the ``fast_mode``
    knob, which is the only value that differs between the fast-path and
    slow-path attempts.
    """

    launch_mode: LaunchMode
    workspace_dir: Path | None
    host_name: HostName
    display_name: str
    on_output: OutputCallback
    latchkey_env: Mapping[str, str] | None
    account_email: str | None
    repo_source: str | None
    branch_or_tag: str | None
    region: str | None
    # Bring-your-own-key account provider block name (``byok-<backend>-<slug>``), or
    # None for the ambient per-mode providers.
    cloud_account: str | None
    # Per-create EC2 machine size (AWS modes only), or None for the block default.
    instance_type: str | None
    parent_cg: ConcurrencyGroup | None
    color: str | None
    docker_runtime: DockerRuntime
    original_minds_version: str | None
    original_branch: str | None
    # Resolved ready pre-baked Lima raw image path, or None to build in-VM.
    prebaked_lima_image_raw_path: Path | None = None
    # Opaque pending-create-attempt id stamped on the host as a ``workspace-id``
    # label (LIMA / DOCKER only), or None for the other modes.
    workspace_id_label: str | None = None


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
        display_name=params.display_name,
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
        cloud_account=params.cloud_account,
        instance_type=params.instance_type,
        color=params.color,
        docker_runtime=params.docker_runtime,
        original_minds_version=params.original_minds_version,
        original_branch=params.original_branch,
        prebaked_lima_image_raw_path=params.prebaked_lima_image_raw_path,
        workspace_id_label=params.workspace_id_label,
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

    Tracks create attempt status so the desktop client can show progress
    and redirect users to agents when the create attempt is complete.

    Thread-safe: all status reads/writes are guarded by an internal lock.
    """

    paths: WorkspacePaths = Field(frozen=True, description="Filesystem paths for minds data")
    server_port: int = Field(
        default=0,
        frozen=True,
        description=(
            "Port the desktop client is listening on. Used to build the absolute "
            "http://<agent-id>.localhost:<port>/ redirect URL after agent create attempt. "
            "The default of 0 is only appropriate for tests that never exercise the "
            "happy-path redirect."
        ),
    )
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        default=None,
        frozen=True,
        description=(
            "Wrapper around `mngr imbue_cloud …`. Used by IMBUE_CLOUD-mode create attempts to mint "
            "a LiteLLM virtual key before the standard ``mngr create`` invocation, and by "
            "destruction to release the lease. The lease + SSH bootstrap + agent rename "
            "themselves run inside the plugin's ``ImbueCloudProvider.create_host``, so minds "
            "no longer maintains its own SuperTokens session, host pool, or LiteLLM key code. "
            "Other launch modes do not consult this client."
        ),
    )
    backup_quota_evictor_factory: Callable[[str], Callable[[], bool] | None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Given an account email, returns the quota-eviction callback backup provisioning "
            "retries with when the bucket create hits a quota limit (or None when the account "
            "is unknown). None disables eviction entirely."
        ),
    )
    latchkey: Latchkey | None = Field(
        default=None,
        frozen=True,
        description=(
            "Latchkey wrapper that owns the shared ``latchkey gateway`` subprocess. When "
            "provided, agent create attempt derives the gateway's shared password and a per-host "
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
    lima_image_gate: LimaImageCreateGate | None = Field(
        default=None,
        frozen=True,
        description=(
            "Pre-baked Lima image create gate. When set and the create matches the default "
            "workspace (Lima + default workspace template repo + current release tag), the create "
            "gates on the verified image and points Lima at it; None disables the path."
        ),
    )
    pending_create_attempt_store: PendingCreateAttemptStore | None = Field(
        default=None,
        frozen=True,
        description=(
            "Durable pending-create-attempt record store. When set, a record carrying the full create "
            "request is written before the ``mngr create`` subprocess spawns, updated on the "
            "terminal states, and (on success) deleted only once discovery confirms the workspace "
            "-- the crash-safe half of the workspace<->account association. None disables the "
            "records (appropriate for tests that don't exercise the reconcile)."
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
            "a workspace create attempt whose ``system-interface`` takes a while to bind ``:8000`` would let the "
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
    on_create_attempts_changed: Callable[[], None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Fired whenever the set of visible create attempt rows changes (a create attempt starts, reaches a "
            "terminal state, or is forgotten). Wired to the backend resolver's notify_change so the "
            "chrome SSE rebuilds the workspace-list payload without waiting for a discovery tick."
        ),
    )
    mngr_binary: str = Field(
        default=MNGR_BINARY,
        frozen=True,
        description=(
            "mngr binary the implicit-discard cleanup subprocesses invoke (listing / destroying a "
            "dead same-name create attempt's leftover host). Tests point this at a fake executable."
        ),
    )

    # In-flight create attempt state is keyed by ``str(CreateAttemptId)`` because the
    # canonical ``AgentId`` doesn't exist until ``mngr create`` returns.
    # Once it does, the corresponding ``CreateAttemptId`` row in
    # ``_canonical_agent_ids`` gets populated and ``AgentCreateAttemptInfo``
    # snapshots include the new ``agent_id`` field.
    _statuses: dict[str, AgentCreateAttemptStatus] = PrivateAttr(default_factory=dict)
    _canonical_agent_ids: dict[str, AgentId] = PrivateAttr(default_factory=dict)
    _redirect_urls: dict[str, str] = PrivateAttr(default_factory=dict)
    _errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _error_kinds: dict[str, CreateAttemptErrorKind] = PrivateAttr(default_factory=dict)
    _launch_modes: dict[str, LaunchMode] = PrivateAttr(default_factory=dict)
    _host_names: dict[str, str] = PrivateAttr(default_factory=dict)
    # Provider instance each create attempt targets (the scope of host-name
    # uniqueness), so in-flight duplicate-name checks match mngr's own
    # per-provider conflict semantics. Empty string when the instance could
    # not be resolved (the create will fail downstream with its own error).
    _provider_instance_names: dict[str, str] = PrivateAttr(default_factory=dict)
    _log_sinks: dict[str, CreateAttemptLogSink] = PrivateAttr(default_factory=dict)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_create_attempt(
        self,
        repo_source: str,
        host_name: str = "",
        display_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.DOCKER,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        cloud_account: str = "",
        instance_type: str = "",
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
        account_id: str = "",
    ) -> CreateAttemptId:
        """Start creating an agent from a git URL or local path in a background thread.

        Raises ``WorkspaceNameInUseError`` when a live (non-terminal) create attempt
        on the same provider instance already holds the requested name --
        mngr's own conflict pre-flight cannot see a create that has not yet
        reserved its host name, so two concurrent minds create attempts would
        otherwise race.

        ``account_id`` is the owning account's user id (empty for a private
        workspace); it is persisted only into the local pending-create-attempt
        record so the startup reconcile can restore the workspace<->account
        association after a crash mid-create.

        No AI credentials are chosen or injected at create time: the
        workspace boots unauthenticated and its in-UI sign-in modal is the
        sole auth surface (subscription setup-token, Imbue LiteLLM key, or
        raw API key -- all written into the shared Claude settings env).
        ``account_email`` still selects the imbue_cloud account for compute
        leasing and backups.

        ``docker_runtime`` selects the container runtime for
        ``LaunchMode.DOCKER`` (runc vs gVisor's runsc); it is ignored by every
        other launch mode, which pin their own runtime.

        For ``LaunchMode.IMBUE_CLOUD``, the agent runs on a leased pool host
        via the ``imbue_cloud_<account-slug>`` provider; the plugin's
        ``ImbueCloudProvider.create_host`` runs the lease + SSH bootstrap
        and the rest of mngr's create pipeline adopts the pool host's
        pre-baked agent under the requested name. The plugin owns the
        SuperTokens session, so minds only needs to know which account to
        ask for.

        When ``on_created`` is provided, it is called with the canonical
        ``AgentId`` and ``HostId`` once ``mngr create`` returns (immediately
        after the status flips to ``DONE``, so consumers can rely on the
        published canonical id). Both ids are parsed from the
        inner ``mngr create``'s JSONL ``"event": "created"`` line, not
        pre-generated; for imbue_cloud agents they are the leased pool
        host's pre-baked ids.

        Returns a ``CreateAttemptId`` immediately for tracking the in-flight
        create attempt. Use ``get_create_attempt_info()`` to poll status (and read
        ``info.agent_id`` once it's populated) or ``get_log_sink()`` to
        stream create attempt logs. The minds-internal ``CreateAttemptId`` and the
        canonical ``AgentId`` are different namespaces by design (different
        ``RandomId`` prefixes) so they can never accidentally be swapped.
        """
        # The replayable buffer retains the last lines put into it so re-entry
        # into the creating page replays history and a FAILED pending-create-attempt
        # record can snapshot the create attempt log's tail.
        log_sink = CreateAttemptLogSink()
        # ``host_name`` falls back to a repo-derived name when blank so the
        # API path (``POST /api/v1/workspaces``) doesn't need to compute it
        # itself. The form path already requires the field. ``HostName(...)`` is
        # invoked downstream in ``_create_agent_background`` so any invalid
        # input fails inside the background thread with an error_message
        # rather than crashing this synchronous entry point.
        effective_name = host_name.strip() if host_name.strip() else extract_repo_name(repo_source)
        # The arbitrary human-readable display name. Falls back to the host-name
        # slug when the caller did not supply a separate display name (e.g. an
        # auto-generated ``workspace-N``).
        effective_display_name = display_name.strip() if display_name.strip() else effective_name
        effective_branch = branch.strip()

        create_attempt_id = CreateAttemptId()

        # Resolve the provider instance this create targets so the in-flight
        # duplicate-name guard scopes exactly like mngr's own per-provider
        # host-name conflict check.
        try:
            provider_instance_name = provider_instance_name_for_launch(
                launch_mode,
                imbue_cloud_account=account_email or None,
                region=region or None,
                cloud_account=cloud_account or None,
            )
        except MngrCommandError as e:
            # Missing account/region context is rejected by the create routes
            # before reaching here; if a caller slips through anyway, the
            # background create fails with the same error, so only the
            # in-flight name guard is skipped.
            logger.debug("Could not resolve a provider instance for create attempt {}: {}", create_attempt_id, e)
            provider_instance_name = ""

        with self._lock:
            # Check-and-register under one lock hold so two concurrent
            # same-name creates cannot both pass the guard.
            if provider_instance_name and effective_name.casefold() in self._live_in_flight_names_locked(
                provider_instance_name
            ):
                raise WorkspaceNameInUseError(
                    f"A workspace named '{effective_name}' is already being created. "
                    "Wait for that create attempt to finish or pick a different name."
                )
            self._statuses[str(create_attempt_id)] = AgentCreateAttemptStatus.INITIALIZING
            self._launch_modes[str(create_attempt_id)] = launch_mode
            self._host_names[str(create_attempt_id)] = effective_name
            self._provider_instance_names[str(create_attempt_id)] = provider_instance_name
            self._log_sinks[str(create_attempt_id)] = log_sink

        # Persist the pending-create-attempt record BEFORE spawning anything: from
        # here on, a crash or quit can no longer orphan the create silently.
        self._write_pending_create_attempt_record(
            create_attempt_id=create_attempt_id,
            provider_instance_name=provider_instance_name,
            repo_source=repo_source,
            host_name=effective_name,
            display_name=effective_display_name,
            branch=effective_branch,
            launch_mode=launch_mode,
            account_id=account_id,
            account_email=account_email,
            branch_or_tag=branch_or_tag,
            region=region,
            cloud_account=cloud_account,
            instance_type=instance_type,
            color=color,
            docker_runtime=docker_runtime,
            original_minds_version=original_minds_version,
            backup_request=backup_request,
        )

        thread = threading.Thread(
            target=self._create_agent_background,
            args=(
                create_attempt_id,
                repo_source,
                effective_name,
                effective_display_name,
                effective_branch,
                log_sink,
                launch_mode,
                account_email,
                branch_or_tag,
                region,
                cloud_account,
                instance_type,
                on_created,
                backup_request,
                color,
                docker_runtime,
                original_minds_version,
            ),
            daemon=True,
            name="agent-creator-{}".format(create_attempt_id),
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)
        self._notify_create_attempts_changed()
        return create_attempt_id

    def wait_for_all(self, timeout: float = 10.0) -> None:
        """Wait for all background create attempt threads to finish."""
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=timeout)

    def _live_in_flight_names_locked(self, provider_instance_name: str | None) -> set[str]:
        """Casefolded host names of live (non-terminal) create attempts. Must hold ``self._lock``.

        ``provider_instance_name`` scopes the result to one provider instance
        (matching mngr's per-provider host-name uniqueness); ``None`` returns
        every live create attempt's name (the cross-provider set the ``workspace-N``
        auto-namer avoids). Terminal (DONE / FAILED) create attempts do not reserve
        their names: a finished workspace is visible to discovery-based checks
        and a dead create attempt's name is free to reuse.
        """
        names: set[str] = set()
        for cid_str, status in self._statuses.items():
            if status in (AgentCreateAttemptStatus.DONE, AgentCreateAttemptStatus.FAILED):
                continue
            if provider_instance_name is not None and self._provider_instance_names.get(cid_str) != (
                provider_instance_name
            ):
                continue
            name = self._host_names.get(cid_str)
            if name:
                names.add(name.casefold())
        return names

    def live_in_flight_host_names(self, provider_instance_name: str | None = None) -> set[str]:
        """Casefolded host names held by live (non-terminal) create attempts.

        Scoped to ``provider_instance_name`` when given (the create form's
        availability check), or across all providers when ``None`` (the
        ``workspace-N`` auto-namer). Thread-safe.
        """
        with self._lock:
            return self._live_in_flight_names_locked(provider_instance_name)

    def live_in_flight_create_attempt_ids(self) -> set[str]:
        """CreateAttempt ids (as strings) of live (non-terminal) create attempts.

        The startup reconcile consults this so a labeled half-built host whose
        create is running in THIS process is never mistaken for an orphan.
        """
        with self._lock:
            return {
                cid_str
                for cid_str, status in self._statuses.items()
                if status not in (AgentCreateAttemptStatus.DONE, AgentCreateAttemptStatus.FAILED)
            }

    def _write_pending_create_attempt_record(
        self,
        create_attempt_id: CreateAttemptId,
        provider_instance_name: str,
        repo_source: str,
        host_name: str,
        display_name: str,
        branch: str,
        launch_mode: LaunchMode,
        account_id: str,
        account_email: str,
        branch_or_tag: str,
        region: str,
        cloud_account: str,
        instance_type: str,
        color: str | None,
        docker_runtime: DockerRuntime,
        original_minds_version: str,
        backup_request: BackupSetupRequest | None,
    ) -> None:
        """Write the IN_FLIGHT pending-create-attempt record, downgrading store errors to warnings.

        The record is the crash-safety net, not a precondition: a disk hiccup
        must not fail an otherwise-valid create, so a write failure only costs
        that net for this one create attempt.
        """
        if self.pending_create_attempt_store is None:
            return
        now = datetime.now(timezone.utc)
        record = PendingCreateAttemptRecord(
            create_attempt_id=str(create_attempt_id),
            state=PendingCreateAttemptState.IN_FLIGHT,
            provider_instance_name=provider_instance_name,
            created_at=now,
            updated_at=now,
            request=PendingCreateAttemptRequest(
                repo_source=repo_source,
                host_name=host_name,
                display_name=display_name,
                branch=branch,
                launch_mode=launch_mode,
                account_id=account_id,
                account_email=account_email,
                branch_or_tag=branch_or_tag,
                region=region,
                cloud_account=cloud_account,
                instance_type=instance_type,
                color=color,
                docker_runtime=docker_runtime,
                original_minds_version=original_minds_version,
                backup_provider=(
                    backup_request.backup_provider if backup_request is not None else BackupProvider.CONFIGURE_LATER
                ),
                backup_api_key_env=(backup_request.api_key_env_text if backup_request is not None else ""),
            ),
        )
        try:
            self.pending_create_attempt_store.write_record(record)
        except PendingCreateAttemptStoreError as e:
            logger.warning("Could not write the pending-create-attempt record for {}: {}", create_attempt_id, e)

    def _mark_pending_create_attempt_done(
        self, create_attempt_id_str: str, agent_id_str: str, host_id_str: str
    ) -> None:
        """Flip the pending record to DONE, downgrading store errors to warnings.

        Same policy as the initial record write: the record is the crash-safety
        net, not a precondition, so a disk hiccup here must not fail (or hang)
        a create whose ``mngr create`` already succeeded.
        """
        if self.pending_create_attempt_store is None:
            return
        try:
            self.pending_create_attempt_store.mark_done(create_attempt_id_str, agent_id_str, host_id_str)
        except PendingCreateAttemptStoreError as e:
            logger.warning(
                "Could not mark the pending-create-attempt record DONE for {}: {}", create_attempt_id_str, e
            )

    def _mark_pending_create_attempt_failed(
        self, create_attempt_id_str: str, error: str, error_kind: str | None, log_tail: tuple[str, ...]
    ) -> None:
        """Flip the pending record to FAILED, downgrading store errors to warnings.

        The in-memory FAILED status is already set by the caller; losing the
        durable failure snapshot only costs the failed row across restarts.
        """
        if self.pending_create_attempt_store is None:
            return
        try:
            self.pending_create_attempt_store.mark_failed(
                create_attempt_id_str, error=error, error_kind=error_kind, log_tail=log_tail
            )
        except PendingCreateAttemptStoreError as e:
            logger.warning(
                "Could not mark the pending-create-attempt record FAILED for {}: {}", create_attempt_id_str, e
            )

    def get_create_attempt_info(self, create_attempt_id: CreateAttemptId) -> AgentCreateAttemptInfo | None:
        """Get the current create attempt status for an in-flight create attempt, or None if not tracked.

        ``info.agent_id`` is ``None`` until the inner ``mngr create``
        returns and emits its JSONL ``"event": "created"`` line, after
        which it's populated with the canonical mngr id. ``info.redirect_url``
        is populated atomically with ``DONE``, so the UI doesn't need to
        wait for ``agent_id`` to know where to redirect.
        """
        cid_str = str(create_attempt_id)
        with self._lock:
            if cid_str not in self._statuses:
                return None
            return self._build_create_attempt_info_locked(cid_str)

    def _build_create_attempt_info_locked(self, cid_str: str) -> AgentCreateAttemptInfo:
        """Assemble the info snapshot for a tracked create attempt. Must hold ``self._lock``."""
        return AgentCreateAttemptInfo(
            create_attempt_id=CreateAttemptId(cid_str),
            agent_id=self._canonical_agent_ids.get(cid_str),
            status=self._statuses[cid_str],
            launch_mode=self._launch_modes.get(cid_str, LaunchMode.DOCKER),
            host_name=self._host_names.get(cid_str, ""),
            provider_instance_name=self._provider_instance_names.get(cid_str, ""),
            redirect_url=self._redirect_urls.get(cid_str),
            error=self._errors.get(cid_str),
            error_kind=self._error_kinds.get(cid_str),
        )

    def list_create_attempt_infos(self) -> list[AgentCreateAttemptInfo]:
        """Snapshot every tracked create attempt (live and terminal), for the create-attempt-rows derivation."""
        with self._lock:
            return [self._build_create_attempt_info_locked(cid_str) for cid_str in self._statuses]

    def forget_create_attempt(self, create_attempt_id: CreateAttemptId) -> bool:
        """Drop a TERMINAL create attempt from the in-memory registry (a dismissed row). Returns whether it was dropped.

        Live create attempts are never forgotten -- their background thread still
        publishes into these maps. A dismissed failed row also deletes its
        pending record (the caller's job); this only clears the in-memory twin
        so the row does not linger until the next app restart.
        """
        cid_str = str(create_attempt_id)
        with self._lock:
            status = self._statuses.get(cid_str)
            if status not in (AgentCreateAttemptStatus.DONE, AgentCreateAttemptStatus.FAILED):
                return False
            for tracked in (
                self._statuses,
                self._canonical_agent_ids,
                self._redirect_urls,
                self._errors,
                self._error_kinds,
                self._launch_modes,
                self._host_names,
                self._provider_instance_names,
                self._log_sinks,
            ):
                tracked.pop(cid_str, None)
        self._notify_create_attempts_changed()
        return True

    def _notify_create_attempts_changed(self) -> None:
        if self.on_create_attempts_changed is not None:
            self.on_create_attempts_changed()

    def get_log_sink(self, create_attempt_id: CreateAttemptId) -> CreateAttemptLogSink | None:
        """Get the replayable log sink for a tracked create attempt, or None if not tracked."""
        with self._lock:
            return self._log_sinks.get(str(create_attempt_id))

    def _create_agent_background(
        self,
        create_attempt_id: CreateAttemptId,
        repo_source: str,
        host_name: str,
        display_name: str,
        branch: str,
        log_sink: CreateAttemptLogSink,
        launch_mode: LaunchMode,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        cloud_account: str = "",
        instance_type: str = "",
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
    ) -> None:
        """Background thread that resolves the repo source and creates an mngr agent.

        No Anthropic credentials are minted or injected here: the workspace
        boots unauthenticated and the in-workspace sign-in modal writes the
        credentials into the shared Claude settings env after first boot.

        For ``LaunchMode.IMBUE_CLOUD``, the plugin's provider backend
        handles the lease + SSH bootstrap inside ``create_host``; the
        canonical agent id is parsed from ``mngr create``'s JSONL
        ``"event": "created"`` line (no follow-up ``mngr list`` lookup --
        which used to fail when the SSH provider had stale dynamic_hosts
        entries).
        """
        cid_str = str(create_attempt_id)
        emit_log = make_log_callback(log_sink)
        workspace_dir: Path | None = None
        try:
            with log_span(
                "Creating agent for create attempt {} from {} (mode: {})",
                create_attempt_id,
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
                # status that ``start_create_attempt`` set; cloning is the first
                # real action. The caption rendered for this status is
                # launch-mode-aware via ``_STATUS_TEXT_IMBUE_CLOUD``.
                with self._lock:
                    self._statuses[cid_str] = AgentCreateAttemptStatus.CLONING_REPO

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
                        log_sink.put("[minds] Cloning local worktree: {}".format(resolved_path))
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
                        # system/vendor/mngr/) are included in the Docker build context.
                        _rsync_worktree_over_clone(
                            resolved_path,
                            clone_target,
                            on_output=emit_log,
                            parent_cg=self.root_concurrency_group,
                        )
                        workspace_dir = clone_target
                        is_workspace_dir_scratch_clone = True
                    else:
                        workspace_dir = resolved_path
                        is_workspace_dir_scratch_clone = False
                        log_sink.put("[minds] Using local directory: {}".format(workspace_dir))
                else:
                    repo_name = extract_repo_name(repo_source)
                    clone_target = Path(tempfile.gettempdir()) / "minds-clone-{}".format(repo_name)
                    if clone_target.exists():
                        shutil.rmtree(clone_target)
                    log_sink.put("[minds] Cloning {}...".format(_redact_url_credentials(repo_source)))
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
                    is_workspace_dir_scratch_clone = True

                if branch:
                    with self._lock:
                        self._statuses[cid_str] = AgentCreateAttemptStatus.CHECKING_OUT_BRANCH
                    log_sink.put("[minds] Checking out branch '{}'...".format(branch))
                    # Scratch clones were just fetched into, so FETCH_HEAD is the
                    # requested ref; a plain local directory has no such fetch, and
                    # is the user's own checkout whose branch tip must not be reset.
                    if is_workspace_dir_scratch_clone:
                        checkout_branch(
                            workspace_dir,
                            GitBranch(branch),
                            on_output=emit_log,
                            parent_cg=self.root_concurrency_group,
                        )
                    else:
                        checkout_existing_branch(
                            workspace_dir,
                            GitBranch(branch),
                            on_output=emit_log,
                            parent_cg=self.root_concurrency_group,
                        )

                with self._lock:
                    self._statuses[cid_str] = AgentCreateAttemptStatus.CREATING_WORKSPACE

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
                latchkey_setup = self._prepare_latchkey_or_warn(log_sink)

                # No prepare step here: a bring-your-own-key account's cloud
                # scaffolding (AWS security group + state bucket, GCP/Azure
                # equivalents) is created once when the account is *added*
                # (``_handle_create_cloud_account``), against the account's pinned
                # placement -- the same region every workspace on it uses. The
                # other modes (docker / lima / vultr / imbue_cloud / modal) need
                # no pre-created scaffolding at all.

                parsed_host = HostName(host_name)
                log_sink.put("[minds] Creating workspace '{}' (mode: {})...".format(host_name, launch_mode.value))

                # A dead (interrupted / failed) earlier create attempt holding this
                # same name on this provider is implicitly discarded before the
                # new create runs: destroy its leftover half-built host (when
                # one exists) and delete its record -- clean up, then try
                # making it again. Provider-scoped: a dead row with the same
                # name on ANOTHER provider neither blocks nor gets destroyed.
                with self._lock:
                    provider_instance_name = self._provider_instance_names.get(cid_str, "")
                self._discard_dead_create_attempts_holding_name(
                    current_create_attempt_id_str=cid_str,
                    provider_instance_name=provider_instance_name,
                    host_name=host_name,
                    log_sink=log_sink,
                )

                # Returns None (build in-VM) for any non-default create, an unpublished
                # version, or a download that stalled or ran out the wait; raises a retryable
                # error only when a published image failed to fetch or verify.
                prebaked_lima_image_raw_path: Path | None = None
                if self.lima_image_gate is not None:
                    is_lima = launch_mode is LaunchMode.LIMA
                    if is_lima:
                        log_sink.put("[minds] Checking for a pre-baked Lima image...")
                    prebaked_lima_image_raw_path = self.lima_image_gate.resolve_image_for_create(
                        is_lima_launch_mode=is_lima,
                        repo_url=repo_source or "",
                        branch_or_tag=branch_or_tag,
                        environ=os.environ,
                        wait_timeout_seconds=_PREBAKED_IMAGE_WAIT_TIMEOUT_SECONDS,
                        poll_interval_seconds=_PREBAKED_IMAGE_POLL_INTERVAL_SECONDS,
                        on_download_progress=_PrebakedImageProgressReporter(log_line=log_sink.put),
                        # Only a Lima create was ever going to use the image, so only it is owed
                        # an explanation for building the workspace the slow way instead.
                        on_fallback_to_in_vm=_PrebakedImageFallbackReporter(log_line=log_sink.put)
                        if is_lima
                        else None,
                    )
                    if prebaked_lima_image_raw_path is not None:
                        log_sink.put("[minds] Using pre-baked Lima image (fast create).")

                # ``fast_mode`` is the only knob that varies between the fast-
                # path and slow-path attempts; bundle the rest of the per-
                # create attempt inputs so each attempt takes just it.
                attempt_params = _MngrCreateAttemptParams(
                    launch_mode=launch_mode,
                    workspace_dir=workspace_dir,
                    host_name=parsed_host,
                    display_name=display_name or str(parsed_host),
                    on_output=emit_log,
                    latchkey_env=latchkey_setup.env,
                    account_email=account_email,
                    repo_source=repo_source,
                    branch_or_tag=branch_or_tag,
                    region=region,
                    cloud_account=cloud_account or None,
                    instance_type=instance_type or None,
                    parent_cg=self.root_concurrency_group,
                    color=color,
                    docker_runtime=docker_runtime,
                    original_minds_version=original_minds_version or None,
                    original_branch=branch or None,
                    prebaked_lima_image_raw_path=prebaked_lima_image_raw_path,
                    # The pending-create-attempt id rides on LIMA / DOCKER hosts as a
                    # ``workspace-id`` host label so the startup reconcile can
                    # re-attach an orphaned host to its record. Modal is excluded
                    # (sandboxes self-expire) and imbue_cloud pool hosts have
                    # their own reconcile.
                    workspace_id_label=(
                        str(create_attempt_id) if launch_mode in (LaunchMode.LIMA, LaunchMode.DOCKER) else None
                    ),
                )

                if launch_mode is LaunchMode.IMBUE_CLOUD:
                    canonical_id, canonical_host_id = self._create_imbue_cloud_with_fallback(attempt_params, log_sink)
                else:
                    canonical_id, canonical_host_id = _attempt_mngr_create(None, attempt_params)

                # Record the canonical ids as soon as they exist: from here a
                # crash can no longer lose the workspace<->record association.
                # The DONE record is only deleted once discovery confirms the
                # workspace (see the resolver sweep in ``cli/run.py``).
                self._mark_pending_create_attempt_done(cid_str, str(canonical_id), str(canonical_host_id))

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
                # rather than failing agent create attempt: the gateway still
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
                        log_sink.put(
                            "[minds] Warning: could not link latchkey permissions handle to "
                            f"canonical path for host {canonical_host_id}; this will be repaired "
                            f"automatically the first time the agent requests a permission. Reason: {link_error}"
                        )

                log_sink.put("[minds] Agent created successfully.")

                # Wait for the agent's system_interface to actually answer 200
                # through the plugin before publishing the redirect. Without
                # this poll, the user gets dropped on a hard error page (404
                # /503) for the few seconds between ``mngr create`` returning
                # and the system_interface inside the agent finishing
                # startup. The probe is best-effort: if it times out, we
                # publish anyway so the user at least lands on the retry
                # page rather than spinning forever (PR 1471 part 1).
                with self._lock:
                    self._statuses[cid_str] = AgentCreateAttemptStatus.WAITING_FOR_READY
                # A Lima create with no pre-baked image builds the workspace
                # inside the VM, which routinely outlives the standard window;
                # give it the build-in-VM window instead. While the wait runs,
                # a create attempt grace suppresses the health tracker's STUCK
                # takeover -- 503s from a still-provisioning workspace are
                # expected, and bouncing the user to the recovery page
                # mid-create is exactly the bug this prevents. The grace ends
                # when the wait returns (probe success, window expiry, or the
                # create attempt going terminal), so a genuinely wedged workspace
                # still reaches STUCK after its window.
                is_build_in_vm_lima = launch_mode is LaunchMode.LIMA and prebaked_lima_image_raw_path is None
                ready_timeout_seconds = (
                    _BUILD_IN_VM_LIMA_READY_TIMEOUT_SECONDS
                    if is_build_in_vm_lima
                    else self.workspace_ready_timeout_seconds
                )
                self.system_interface_health_tracker.begin_create_attempt_grace(
                    canonical_id, time.monotonic() + ready_timeout_seconds
                )
                try:
                    self._wait_for_workspace_ready(canonical_id, log_sink, ready_timeout_seconds)
                finally:
                    self.system_interface_health_tracker.end_create_attempt_grace(canonical_id)

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
                # downstream consumer (e.g. ``OnCreatedCallback``, which kicks
                # off the Cloudflare tunnel + workspace association) can rely on
                # the canonical id.
                with self._lock:
                    self._canonical_agent_ids[cid_str] = canonical_id
                    self._statuses[cid_str] = AgentCreateAttemptStatus.DONE
                    self._redirect_urls[cid_str] = redirect_url
                self._notify_create_attempts_changed()

                if on_created is not None:
                    on_created(canonical_id, canonical_host_id)

                # Configure restic backups asynchronously on a detached
                # thread (mirrors the Cloudflare tunnel-token path): bucket
                # create attempt + injection is a multi-second round-trip we don't
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
            logger.opt(exception=e).error("Failed to create agent for create attempt {}", create_attempt_id)
            log_sink.put("[minds] ERROR: {}".format(e))
            error_kind = classify_create_attempt_error(repo_source, e)
            with self._lock:
                self._statuses[cid_str] = AgentCreateAttemptStatus.FAILED
                self._errors[cid_str] = str(e)
                if error_kind is not None:
                    self._error_kinds[cid_str] = error_kind
            # Snapshot the failure (and the create attempt log's tail) into the
            # pending-create-attempt record so the failed row survives restarts.
            self._mark_pending_create_attempt_failed(
                cid_str,
                error=str(e),
                error_kind=error_kind.value if error_kind is not None else None,
                log_tail=log_sink.tail_lines(FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES),
            )
            self._notify_create_attempts_changed()
        finally:
            log_sink.put(LOG_SENTINEL)

    def _discard_dead_create_attempts_holding_name(
        self,
        current_create_attempt_id_str: str,
        provider_instance_name: str,
        host_name: str,
        log_sink: CreateAttemptLogSink,
    ) -> None:
        """Implicitly discard dead same-name create attempts on this provider before a fresh create.

        A dead create attempt is a pending record that is not DONE and has no live
        create attempt behind it (interrupted by a restart, or failed). Its leftover
        half-built host -- found through the ``workspace-id`` host label on the
        labeled providers -- is destroyed and its record deleted, so the fresh
        create does not trip mngr's host-name conflict pre-flight. A failed
        cleanup keeps the record (its row stays for a manual discard) and lets
        the create proceed to mngr's own conflict error.
        """
        if self.pending_create_attempt_store is None or not provider_instance_name:
            return
        live_create_attempt_ids = self.live_in_flight_create_attempt_ids()
        dead_records = [
            record
            for record in self.pending_create_attempt_store.list_records()
            if record.create_attempt_id != current_create_attempt_id_str
            and record.create_attempt_id not in live_create_attempt_ids
            and record.state is not PendingCreateAttemptState.DONE
            and record.provider_instance_name == provider_instance_name
            and record.request.host_name.casefold() == host_name.casefold()
        ]
        if not dead_records:
            return

        # Only the labeled providers can have a findable leftover host; the
        # other providers' dead records are deleted record-only.
        mngr_env = dict(os.environ)
        leftover_hosts: list[ListedHost] = []
        if provider_instance_name in WORKSPACE_ID_LABELED_PROVIDER_NAMES:
            try:
                leftover_hosts = list_provider_hosts(
                    self.root_concurrency_group,
                    self.mngr_binary,
                    mngr_env,
                    provider_instance_name,
                    timeout_seconds=_DEAD_CREATE_ATTEMPT_HOST_LIST_TIMEOUT_SECONDS,
                )
            except MngrCommandError as e:
                logger.warning(
                    "Could not list {} hosts to discard a dead create attempt: {}", provider_instance_name, e
                )
                log_sink.put(
                    "[minds] Warning: could not check for a leftover host from a previous attempt; "
                    f"continuing anyway: {e}"
                )
                return

        for record in dead_records:
            leftover = find_host_by_workspace_id_label(leftover_hosts, record.create_attempt_id)
            if leftover is not None:
                log_sink.put(
                    f"[minds] Cleaning up the previous attempt's unfinished host '{leftover.name}' ({leftover.id})..."
                )
                destroy_argv = [self.mngr_binary, "destroy", f"@{leftover.id}.{leftover.provider}", "--force"]
                try:
                    run_mngr_to_completion(
                        self.root_concurrency_group,
                        destroy_argv,
                        mngr_env,
                        timeout_seconds=_DEAD_CREATE_ATTEMPT_HOST_DESTROY_TIMEOUT_SECONDS,
                    )
                except MngrCommandError as e:
                    logger.warning(
                        "Could not destroy leftover host {} for dead create attempt {}: {}",
                        leftover.id,
                        record.create_attempt_id,
                        e,
                    )
                    log_sink.put(f"[minds] Warning: could not remove the previous attempt's host {leftover.id}: {e}")
                    continue
            self.pending_create_attempt_store.delete_record(record.create_attempt_id)
            self.forget_create_attempt(CreateAttemptId(record.create_attempt_id))
            logger.info(
                "Implicitly discarded dead create attempt {} (name '{}' reused)", record.create_attempt_id, host_name
            )
        self._notify_create_attempts_changed()

    def _create_imbue_cloud_with_fallback(
        self,
        attempt_params: _MngrCreateAttemptParams,
        log_sink: CreateAttemptLogSink,
    ) -> tuple[AgentId, HostId]:
        """Try the fast (adopt) path, then fall back to the slow (rebuild) path.

        The first attempt requests ``fast_mode=require`` -- the imbue_cloud
        provider adopts a pre-baked pool host whose attributes exactly match.
        If none is available the provider raises ``FastPathUnavailableError``,
        which ``mngr create --format jsonl`` surfaces as a structured
        ``{"event": "error", "error_class": "FastPathUnavailableError"}`` line;
        minds matches on that ``error_class`` and retries with
        ``fast_mode=prevent``, which leases any available host and rebuilds it
        from the DEFAULT_WORKSPACE_TEMPLATE Dockerfile (full client-side setup). Any other failure
        (including a genuinely empty pool) propagates unchanged.
        """
        log_sink.put("[minds] Trying fast path (adopt a matching pre-baked pool host)...")
        try:
            return _attempt_mngr_create(_FAST_MODE_REQUIRE, attempt_params)
        except MngrCommandError as exc:
            if exc.error_class != _FAST_PATH_UNAVAILABLE_ERROR_CLASS:
                raise
            logger.info("imbue_cloud fast path unavailable; retrying with the slow path (full rebuild)")
            log_sink.put(
                "[minds] No matching pre-baked pool host; falling back to slow path (leasing any host "
                "and rebuilding it). This is slower but always works when the pool has free hosts..."
            )
            return _attempt_mngr_create(_FAST_MODE_PREVENT, attempt_params)

    def _prepare_latchkey_or_warn(
        self,
        log_sink: CreateAttemptLogSink,
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
            log_sink.put("[minds] Warning: latchkey wiring skipped: {}".format(e))
            return AgentLatchkeySetup(env={}, opaque_permissions_path=None)
        except LatchkeyStoreError as e:
            logger.warning("Failed to materialize latchkey permissions handle: {}", e)
            log_sink.put("[minds] Warning: latchkey wiring skipped: {}".format(e))
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
            # A structured quota refusal is deterministic (retrying cannot
            # succeed), so it is excluded from the retry predicate and falls
            # straight through to the notification below.
            quota_evictor = (
                self.backup_quota_evictor_factory(backup_request.account_email)
                if self.backup_quota_evictor_factory is not None and backup_request.account_email
                else None
            )
            for attempt in Retrying(
                retry=retry_if_exception_type((BackupProvisioningError, ImbueCloudCliError))
                & retry_if_not_exception_type(ImbueCloudQuotaExceededCliError),
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
                        quota_evictor=quota_evictor,
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
        """Build the absolute URL the UI should navigate to after the create attempt.

        Always points at the plugin's ``/goto/<agent>/`` route, never minds'
        bare origin -- minds doesn't serve ``/goto/`` and would 404. When
        ``mngr_forward_port`` isn't configured (test fixtures, etc.), falls
        back to the relative form so legacy callers that don't set the field
        keep working.
        """
        if self.mngr_forward_port == 0:
            return f"/goto/{agent_id}/"
        return f"{_MNGR_FORWARD_SCHEME}://localhost:{self.mngr_forward_port}/goto/{agent_id}/"

    def _wait_for_workspace_ready(
        self,
        agent_id: AgentId,
        log_sink: CreateAttemptLogSink,
        # The readiness window for this create: ``workspace_ready_timeout_seconds``
        # normally, or the longer build-in-VM window for an imageless Lima create.
        timeout_seconds: float,
    ) -> None:
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
        retry page, which is better than spinning forever in the create attempt UI.
        """
        if self.mngr_forward_port == 0 or not self.mngr_forward_preauth_cookie:
            logger.debug("Workspace readiness probe disabled (port=0 or empty preauth); skipping")
            return

        deadline = time.monotonic() + timeout_seconds
        log_sink.put("[minds] Waiting for system interface to be ready...")
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
                        log_sink.put("[minds] System interface is ready.")
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
            timeout_seconds,
            last_status,
        )
        log_sink.put(
            "[minds] Warning: workspace did not become ready within "
            f"{timeout_seconds:.0f}s; you may see a retry page on first load."
        )
