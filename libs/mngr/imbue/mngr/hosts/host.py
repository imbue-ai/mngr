from __future__ import annotations

import fcntl
import importlib.resources
import io
import json
import math
import os
import shlex
import tempfile
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final
from typing import Iterator
from typing import Mapping
from typing import Sequence
from typing import assert_never
from uuid import uuid4

from loguru import logger
from paramiko import SSHException
from paramiko import Transport
from pydantic import Field
from pydantic import ValidationError
from pyinfra.api.command import StringCommand
from pyinfra.connectors.util import CommandOutput
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_chain
from tenacity import wait_fixed

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.logging import info_span
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr import resources as mngr_resources
from imbue.mngr.config.agent_class_registry import get_orphan_agent_class
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import CommandTimeoutError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostDataSchemaError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import InvalidActivityTypeError
from imbue.mngr.errors import LockNotHeldError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import UnknownAgentTypeError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import get_agents_root_dir
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.hosts.file_upload import upload_files_in_bulk
from imbue.mngr.hosts.offline_host import BaseHost
from imbue.mngr.hosts.offline_host import apply_rename_to_agent_data
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.cleanup_failures import collect_cleanup_failures
from imbue.mngr.interfaces.cleanup_failures import collecting_cleanup_failures
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.host import AgentTmuxOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import CreateWorkDirResult
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.host import PROVISIONING_FIELD_MAP
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.deps import SSH
from imbue.mngr.utils.env_utils import build_source_env_shell_commands
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr.utils.git_utils import GIT_MIRROR_PUSH_REFSPECS
from imbue.mngr.utils.name_generator import GENERIC_AGENT_NAME_HINT
from imbue.mngr.utils.polling import wait_for


@pure
def _merge_agent_type_provisioning(
    agent_config: AgentTypeConfig,
    options: CreateAgentOptions,
) -> CreateAgentOptions:
    """Merge provisioning fields from an agent type config into CreateAgentOptions.

    Parses raw string specs from AgentTypeConfig into typed specs and prepends them
    before the CLI-provided entries so that agent type provisioning runs first and
    CLI entries can override (e.g., env vars with the same key).

    Returns the original options unchanged if the agent config has no provisioning fields.
    """
    prov_updates: list[tuple[str, Any]] = []
    for config_field, target_field, parser in PROVISIONING_FIELD_MAP:
        raw_values: tuple[str, ...] = getattr(agent_config, config_field)
        if raw_values:
            existing: tuple[Any, ...] = getattr(options.provisioning, target_field)
            prov_updates.append((target_field, tuple(parser(s) for s in raw_values) + existing))

    env_vars = tuple(EnvVar.from_string(s) for s in agent_config.env) if agent_config.env else ()
    env_files = tuple(Path(s) for s in agent_config.env_file) if agent_config.env_file else ()

    if not prov_updates and not env_vars and not env_files:
        return options

    updates: list[tuple[str, Any]] = []
    if prov_updates:
        updates.append(
            (
                "provisioning",
                options.provisioning.model_copy_update(*prov_updates),
            )
        )
    env_updates: list[tuple[str, Any]] = []
    if env_vars:
        env_updates.append(("env_vars", env_vars + options.environment.env_vars))
    if env_files:
        env_updates.append(("env_files", env_files + options.environment.env_files))
    if env_updates:
        updates.append(("environment", options.environment.model_copy_update(*env_updates)))
    return options.model_copy_update(*updates)


def _try_acquire_flock(lock_file: io.TextIOWrapper) -> bool:
    """Try to acquire an exclusive flock without blocking. Returns True if acquired."""
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@pure
def _is_transient_ssh_error(exception: BaseException) -> bool:
    """Check if the exception is a transient SSH connection error worth retrying.

    Matches:
    - OSError with "Socket is closed" (stale socket from pyinfra)
    - SSHException (e.g. "SSH session not active" when transport dies),
      including ChannelException (server refused to open a new channel,
      e.g. MaxSessions limit -- the transport may still be alive)
    - EOFError (remote end closed connection)
    - TimeoutError (pyinfra read_output_buffers timeout when the remote
      sshd is reloaded mid-command, e.g. during cloud-init bootstrap).
      Note: ``TimeoutError`` is an OSError subclass on Python 3, so this
      check must precede any narrower OSError handling.
    """
    if isinstance(exception, OSError) and "Socket is closed" in str(exception):
        return True
    if isinstance(exception, SSHException):
        return True
    if isinstance(exception, EOFError):
        return True
    if isinstance(exception, TimeoutError):
        return True
    return False


# Shared retry decorator for file operations that encounter transient SSH
# connection errors.  Retries after (0, 1, 3, 6) seconds for a total
# backoff window of ~10 seconds.
_retry_on_transient_ssh_error = retry(
    retry=retry_if_exception(_is_transient_ssh_error),
    stop=stop_after_attempt(5),
    wait=wait_chain(
        wait_fixed(0),
        wait_fixed(1),
        wait_fixed(3),
        wait_fixed(6),
    ),
    reraise=True,
)


def _get_ssh_transport(pyinfra_host: Any) -> Transport | None:
    """Extract the paramiko Transport from a pyinfra host, or None for non-SSH connectors."""
    try:
        client = pyinfra_host.connector.client
    except AttributeError:
        return None
    if client is not None:
        return client.get_transport()
    return None


# The single host lock file. It is held via a real flock(2) -- directly on local
# hosts, and over a long-lived SSH exec channel on remote hosts -- so a holder
# running locally inside the host (e.g. a VM/container boot hook) and a holder
# running remotely over SSH (e.g. the minds desktop client) mutually exclude.
# It is never deleted so its inode stays stable across local and remote holders;
# the idle-shutdown watcher and ``is_lock_held`` detect it via a flock probe, not
# via file existence.
_HOST_LOCK_FILENAME: Final[str] = "host_lock"

# Default timeout for callers that want a bounded wait (e.g. gc). ``create`` and
# ``start`` pass ``None`` to block indefinitely until the lock is acquired.
_DEFAULT_HOST_LOCK_TIMEOUT_SECONDS: Final[float] = 300.0

# Env var that retains a failed host (and keeps its lock held) for debugging.
_RETAIN_LOCK_FOR_DEBUG_ENV_VAR: Final[str] = "MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE"

# Markers printed by the remote lock holder so the local side can track progress.
_LOCK_ACQUIRED_MARKER: Final[str] = "__MNGR_LOCK_ACQUIRED__"
_LOCK_TIMED_OUT_MARKER: Final[str] = "__MNGR_LOCK_TIMED_OUT__"
_LOCK_WAITING_MARKER: Final[str] = "__MNGR_LOCK_WAITING__"

# Confirmation printed by the detached debug lock holder once it has been forked.
_LOCK_HOLDER_LAUNCHED_MARKER: Final[str] = "__MNGR_LOCK_HOLDER_LAUNCHED__"


def _is_retain_lock_for_debug_enabled() -> bool:
    """Whether the debug flag that keeps a failed host (and its lock) alive is set."""
    return os.environ.get(_RETAIN_LOCK_FOR_DEBUG_ENV_VAR) == "1"


@pure
def _build_remote_lock_command(lock_file_path: Path, timeout_seconds: float | None) -> str:
    """Build the remote shell command that holds a flock(2) until stdin closes.

    Opens the lock fd, tries a non-blocking acquire first (printing a "waiting"
    marker if it must block), waits for the lock (bounded by ``timeout_seconds``
    when given), prints the acquired marker, then reads stdin until EOF. Closing
    the controlling channel sends EOF, so the shell exits, the fd closes, and the
    lock releases. A bounded-wait timeout prints the timeout marker and exits 1;
    any other failure (e.g. ``flock`` missing) exits without printing a marker so
    the local side surfaces it as an unexpected error rather than a timeout.
    """
    quoted_path = shlex.quote(str(lock_file_path))
    quoted_dir = shlex.quote(str(lock_file_path.parent))
    acquired = shlex.quote(_LOCK_ACQUIRED_MARKER)
    timed_out = shlex.quote(_LOCK_TIMED_OUT_MARKER)
    waiting = shlex.quote(_LOCK_WAITING_MARKER)
    hold = f"printf '%s\\n' {acquired} && while IFS= read -r _; do :; done"
    if timeout_seconds is None:
        # Block indefinitely. ``flock -n`` first so we only emit the waiting marker
        # under genuine contention; ``flock 9`` then blocks until released.
        blocking_acquire = "flock 9"
    else:
        # flock -w expects whole seconds; round up so a sub-second budget still
        # waits at least one second.
        wait_seconds = max(1, math.ceil(timeout_seconds))
        blocking_acquire = f"flock -w {wait_seconds} 9"
    return (
        f"mkdir -p {quoted_dir} && exec 9>{quoted_path} && "
        f"if flock -n 9; then {hold}; "
        f"else printf '%s\\n' {waiting} && "
        f"if {blocking_acquire}; then {hold}; "
        f'else rc=$?; [ "$rc" = 1 ] && printf \'%s\\n\' {timed_out}; exit "$rc"; fi; fi'
    )


def _wait_for_remote_lock_acquired(channel: Any) -> None:
    """Block until the remote lock holder reports it has acquired the lock.

    Raises LockNotHeldError if a bounded wait timed out. Raises HostError if the
    channel closes before any result marker (e.g. ``flock`` missing on the host),
    which should never happen and must not be mistaken for a timeout.
    """
    acquired_bytes = _LOCK_ACQUIRED_MARKER.encode()
    timed_out_bytes = _LOCK_TIMED_OUT_MARKER.encode()
    waiting_bytes = _LOCK_WAITING_MARKER.encode()
    buffer = b""
    is_waiting_logged = False
    while acquired_bytes not in buffer:
        if timed_out_bytes in buffer:
            raise LockNotHeldError("Timed out waiting to acquire the host lock")
        if waiting_bytes in buffer and not is_waiting_logged:
            logger.info("Waiting to acquire host lock (another operation is using this host)...")
            is_waiting_logged = True
        chunk = channel.recv(4096)
        if not chunk:
            raise HostError("Remote host lock command exited before acquiring the lock (is flock installed?)")
        buffer += chunk


def install_packaged_script_on_host(
    host: OnlineHostInterface,
    *,
    module: Any,
    filename: str,
    dest: Path,
    mode: str = "0755",
) -> None:
    """Read ``filename`` from a Python package's resources and write it onto ``host``.

    Common per-agent provisioning pattern: a plugin ships a shell or Python
    script as a package resource (under ``<package>/resources/``) and needs
    to install it onto an agent's host (local or remote) so something on
    that host can later execute it. ``host.write_file`` is host-portable
    (works for the local filesystem, SSH'd hosts, Modal volumes, etc.) and
    handles the executable-bit via the ``mode`` argument.

    ``module`` is the package object (e.g. ``imbue.mngr_claude_usage.resources``);
    ``filename`` is the file name inside it; ``dest`` is the absolute path on
    the host where the script should land.
    """
    content = importlib.resources.files(module).joinpath(filename).read_text().encode()
    host.write_file(dest, content, mode=mode)


def read_json_dict_via_host(host: OnlineHostInterface, path: Path) -> dict[str, Any]:
    """Host-aware variant of ``mngr.utils.file_utils.read_json_dict``.

    Reads ``path`` via the host (works for local or remote hosts). Missing
    file, unparseable JSON, or non-object JSON each yield ``{}`` -- the same
    tolerance ``read_json_dict`` provides for plugin provisioning that
    reads optional user-managed config like ``.claude/settings.json``.

    Lives here rather than in ``file_utils`` because it needs
    ``OnlineHostInterface``, which would create a circular import via
    ``config.data_types``.
    """
    try:
        content = host.read_text_file(path)
    except FileNotFoundError:
        return {}
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("Could not parse {} as JSON ({}); treating as empty.", path, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json_dict_via_host(
    host: OnlineHostInterface, path: Path, data: dict[str, Any], *, make_parent: bool = False
) -> None:
    """Write-side counterpart to ``read_json_dict_via_host``.

    Serializes ``data`` as pretty-printed JSON (two-space indent, trailing
    newline) and writes it to ``path`` via the host (works for local or
    remote hosts). When ``make_parent`` is True, creates the parent directory
    first so the write does not depend on something else having created it.

    Lives here rather than in ``file_utils`` because it needs
    ``OnlineHostInterface``, which would create a circular import via
    ``config.data_types``.
    """
    text = json.dumps(data, indent=2) + "\n"
    if make_parent:
        host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(path.parent))}", timeout_seconds=5.0)
    host.write_text_file(path, text)


def _git_command_stdout(host: OnlineHostInterface, command: str, cwd: Path) -> str | None:
    """Run a git command on a host and return its stripped stdout, or None if it failed or was empty.

    Used to read git metadata (current branch, user.name, origin URL, etc.) without
    branching on whether the host is local or remote.
    """
    result = host.execute_idempotent_command(command, cwd=cwd)
    if not result.success:
        return None
    return result.stdout.strip() or None


@pure
def _is_same_machine(a: OnlineHostInterface, b: OnlineHostInterface) -> bool:
    """Whether ``a`` and ``b`` share a filesystem so file ops do not need SSH.

    True when the two hosts share a ``host_id``, or when both are local
    (any two local hosts share the laptop's filesystem regardless of id).
    """
    if a.id == b.id:
        return True
    return a.is_local and b.is_local


# mngr's preferred length of tmux's status-left.
_TMUX_STATUS_LEFT_LENGTH: Final[int] = 20

# Format tmux uses for the outer terminal's tab title (set-titles-string):
# session name then pane title, e.g. 'mngr-foo  Fix the bug'.
_TMUX_SET_TITLES_STRING: Final[str] = "#S  #T"

# Per-command timeout for the individual shell steps that make up the
# stop/cleanup path (tmux list-windows/list-panes/kill-session, the pgrep
# descendant walk, and the MNGR_AGENT_ID env scan). A wedged tmux client can
# hang indefinitely -- tmux occasionally fails to return under CI load, which
# is why the test-cleanup helpers in utils/testing.py already bound every tmux
# subprocess. Without a bound here, a single stuck `tmux list-panes` blocks
# stop_agents forever (observed hanging an entire offload batch). On timeout the
# step is recorded as a TIMEOUT cleanup failure (see _run_classified_cleanup_command)
# rather than hanging or being silently swallowed. These commands normally return
# near-instantly; the bound is generous headroom (including for slower remote hosts)
# before declaring a command wedged.
_STOP_AGENT_COMMAND_TIMEOUT_SECONDS: Final[float] = 10.0

# Lowercased stderr substrings that mark a *benign* stop-command failure: the target
# resource was already gone, so nothing is left behind. A non-empty stderr line that
# matches none of the relevant set is treated as a real failure (see
# Host._classify_cleanup_command_stderr and specs/cleanup-error-aggregation.md).
#
# tmux emits one of these when its target session/window/pane is already gone, or when the
# server itself is absent or exiting -- all of which mean there is nothing left to clean up:
#   - "can't find session/window/pane": the target is gone but the server is still up.
#   - "no server running" / "error connecting to ...": the server socket is absent (the
#     client_connect form, e.g. "error connecting to /tmp/.../default (No such file or
#     directory)", is the common shape on both macOS and Linux; the literal "no server
#     running" string does not always appear).
#   - "lost server" / "server exited": the client was connected but the server went away
#     mid-command. This is a normal teardown race: killing an agent's processes can close
#     its (last) session, which exits the local server just as a concurrent ``kill-session``
#     for a sibling agent runs (e.g. destroying several local agents in parallel).
_TMUX_BENIGN_STDERR_SUBSTRINGS: Final[tuple[str, ...]] = (
    "can't find session",
    "can't find window",
    "can't find pane",
    "no server running",
    "error connecting to",
    "lost server",
    "server exited",
    # When destroying several agents at once, killing the last session makes the tmux
    # server exit; a concurrent kill-session against an already-gone session/server can
    # then fail to resolve its target and report "no current target". The session is gone
    # either way, so this is benign (it surfaced as a spurious LOCAL_STATE_REMAINS before).
    "no current target",
)
# kill(1) emits this (ESRCH) when the target process is already dead -- expected, since
# pids routinely die between collection and the kill loop.
_KILL_BENIGN_STDERR_SUBSTRINGS: Final[tuple[str, ...]] = ("no such process",)

# Default tmux window dimensions used when the agent does not specify its own.
# These match the historical hard-coded ``-x 200 -y 50`` (see the new-session
# call in _build_start_agent_shell_command for why -x/-y are passed at all).
_DEFAULT_TMUX_WIDTH: Final[int] = 200
_DEFAULT_TMUX_HEIGHT: Final[int] = 50


class Host(OuterHost, BaseHost, OnlineHostInterface):
    """Host implementation that proxies operations through a pyinfra connector.

    All operations (command execution, file read/write) are performed through
    the pyinfra connector, which handles both local and remote hosts transparently.

    Inherits the safe-method primitives (file ops, command execution, SSH info)
    from ``OuterHost``. Adds the agent / lifecycle / snapshot / tag machinery
    that distinguishes a managed host from a raw outer host.
    """

    provider_instance: ProviderInstanceInterface = Field(
        frozen=True, description="The provider instance managing this host"
    )
    host_name: HostName = Field(
        frozen=True,
        description=(
            "User-facing name of the host. Stored explicitly because the SSH "
            "connector's name may be a connection target (e.g. an IP for "
            "local-docker hosts) rather than a HostName-shaped value."
        ),
    )
    # ``pre_baked_agent_id`` is inherited from ``HostInterface``; defaults to
    # ``None`` for every Host except ones whose provider populates it (today:
    # ``ImbueCloudHost`` via its lease/adopt flow). The duplicate-agent-name
    # check in ``api/create.py`` uses it to recognize the adopt scenario.

    def get_name(self) -> HostName:
        """Return the user-facing host name (overrides ``OuterHost.get_name``)."""
        return self.host_name

    # is_local, _ensure_connected, _close_paramiko_client, disconnect, and
    # __del__ are inherited unchanged from OuterHost.

    def model_copy_update(self, *updates: Any) -> "Host":
        """Create a copy of this Host with updated fields.

        The copy shares the same pyinfra connector (and thus the same SSH
        client). Mark ourselves so __del__ does not close the shared client
        when this original is garbage collected.
        """
        result = super().model_copy_update(*updates)
        self._explicitly_disconnected = True
        return result

    @contextmanager
    def _notify_on_connection_error(self) -> Iterator[None]:
        """Context manager that calls on_connection_error when HostConnectionError is raised.

        Wraps operations that may raise HostConnectionError. When one is raised, this
        notifies the provider instance before re-raising the exception.
        """
        try:
            yield
        except HostConnectionError:
            self.provider_instance.on_connection_error(self.id)
            raise

    def _run_shell_command(
        self,
        command: StringCommand,
        *,
        _timeout: int | None = None,
        _success_exit_codes: tuple[int, ...] | None = None,
        _env: dict[str, str] | None = None,
        _chdir: str | None = None,
        _shell_executable: str = "sh",
        # Su config
        _su_user: str | None = None,
        _use_su_login: bool = False,
        _su_shell: str | None = None,
        _preserve_su_env: bool = False,
        # Sudo config
        _sudo: bool = False,
        _sudo_user: str | None = None,
        _use_sudo_login: bool = False,
        _sudo_password: str = "",
        _sudo_askpass_path: str | None = None,
        _preserve_sudo_env: bool = False,
        # Doas config
        _doas: bool = False,
        _doas_user: str | None = None,
        # Retry config
        _retries: int = 0,
        _retry_delay: int = 0,
        _retry_until: str | None = None,
        # Timeout handling
        _raise_on_timeout: bool = False,
    ) -> tuple[bool, CommandOutput]:
        """
        Execute a shell command on the host.

        This is an internal-only method, in case you need to do something fancy

        Prefer using execute_command() instead whenever possible.

        When ``_raise_on_timeout`` is set, a local timeout raises
        ``ProcessTimeoutError`` (the remote SSH path already raises
        ``socket.timeout`` on its own), so opt-in callers see a timeout as a hard
        failure on both backends rather than an ordinary failed result.
        """
        if self.is_local:
            # Bypass pyinfra's LocalConnector, which spawns local processes via
            # gevent. gevent attaches a libev SIGCHLD child watcher to the
            # thread-local Hub on first use; on Linux these can only attach to
            # the default event loop, so any local-host command issued from a
            # worker thread raises "child watchers are only available on the
            # default loop". We don't need gevent here, so we run the command
            # via the ConcurrencyGroup's process runner instead.
            if _su_user is not None or _sudo or _doas:
                raise NotImplementedError("Local host shell command bypass does not support _su_user, _sudo, or _doas")
            return self._run_shell_command_local(
                command,
                _timeout=_timeout,
                _success_exit_codes=_success_exit_codes,
                _env=_env,
                _chdir=_chdir,
                _shell_executable=_shell_executable,
                _raise_on_timeout=_raise_on_timeout,
            )
        pyinfra_kwargs: dict[str, Any] = {
            "_timeout": _timeout,
            "_success_exit_codes": _success_exit_codes,
            "_env": _env,
            "_chdir": _chdir,
            "_shell_executable": _shell_executable,
            "_su_user": _su_user,
            "_use_su_login": _use_su_login,
            "_su_shell": _su_shell,
            "_preserve_su_env": _preserve_su_env,
            "_sudo": _sudo,
            "_sudo_user": _sudo_user,
            "_use_sudo_login": _use_sudo_login,
            "_sudo_password": _sudo_password,
            "_sudo_askpass_path": _sudo_askpass_path,
            "_preserve_sudo_env": _preserve_sudo_env,
            "_doas": _doas,
            "_doas_user": _doas_user,
            "_retries": _retries,
            "_retry_delay": _retry_delay,
            "_retry_until": _retry_until,
        }
        with self._notify_on_connection_error():
            try:
                return self._run_shell_command_with_transient_retry(command, pyinfra_kwargs)
            except TimeoutError as e:
                # ``TimeoutError`` is a subclass of ``OSError``, so this
                # must precede the OSError branch below. Reached when the
                # retry decorator has exhausted its attempts on transient
                # SSH read timeouts; surface as a structured
                # HostConnectionError so callers don't see a raw timeout.
                raise HostConnectionError("SSH command timed out reading output") from e
            except OSError as e:
                if "Socket is closed" in str(e):
                    raise HostConnectionError("Connection was closed while running command") from e
                else:
                    raise
            except (EOFError, SSHException) as e:
                raise HostConnectionError("Could not execute command due to connection error") from e

    # _run_shell_command_with_transient_retry and _run_shell_command_local
    # are inherited unchanged from OuterHost. _get_file*, _put_file*,
    # _get_paramiko_transport, _create_sftp_client are also inherited.

    # =========================================================================
    # Convenience methods (built on core primitives)
    # =========================================================================

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        """Execute a command and return the result.

        Note: the underlying _run_shell_command retries on transient SSH errors,
        so commands passed here are assumed to be idempotent.

        By default a timeout is reported like any other failed command
        (``success=False`` on local; the remote SSH layer's ``socket.timeout``
        propagates as-is, preserving prior behavior). When ``raise_on_timeout``
        is set, a timeout on either backend is normalized into a single loud
        ``CommandTimeoutError`` (a ``MngrError``) instead -- for callers that must
        not silently treat a wedged command as "no output".
        """
        logger.trace("Executing command on host {}: {}", self.id, command)
        logger.trace(
            "Resolved command parameters: user={}, cwd={}, env={}, timeout={}", user, cwd, env, timeout_seconds
        )
        try:
            success, output = self._run_shell_command(
                StringCommand(command),
                _su_user=user,
                _chdir=str(cwd) if cwd else None,
                _env=dict(env) if env else None,
                _timeout=int(timeout_seconds) if timeout_seconds else None,
                _raise_on_timeout=raise_on_timeout,
            )
        except (ProcessTimeoutError, TimeoutError) as e:
            # ProcessTimeoutError: local backend (only when raise_on_timeout).
            # TimeoutError: remote SSH socket.timeout (raised regardless of the
            # flag). Re-raise unchanged unless the caller opted into the loud,
            # typed CommandTimeoutError.
            if not raise_on_timeout:
                raise
            raise CommandTimeoutError(f"Command timed out after {timeout_seconds}s: {command}") from e
        return CommandResult(
            stdout=output.stdout,
            stderr=output.stderr,
            success=success,
        )

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """
        Execute a shell command on this host *that cannot be retried* and return the result.

        Prefer to use execute_idempotent_command whenever possible, as it is a much simpler abstraction and more robust.
        This is really here if you *must* do something which cannot be made idempotent.
        It automatically handles making the command idempotent, but it's much slower and more complex.
        """
        # FIXME: actually implement this. It's rather complex:
        #  we need to create a unique lock file, ship the command over, and run an idempotent command that waits for it to be finished
        #  once the command finishes, we can run idempotent commands to fetch the resulting stdout, stderr, and exit code
        #  (which means we need a wrapper that appropriately saves that data somewhere that we can retrieve it)
        #  then, just to be good, we should probably clean up after ourselves (the outputs and lock file)
        return self.execute_idempotent_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    # read_file, write_file, read_text_file, write_text_file, _get_file_mtime,
    # and get_file_mtime are inherited unchanged from OuterHost.

    def _is_directory(self, path: Path) -> bool:
        """Check if a path is a directory on the host."""
        if self.is_local:
            return path.is_dir()
        result = self.execute_idempotent_command(f"test -d '{str(path)}'")
        return result.success

    def _list_directory(self, path: Path) -> list[str]:
        """List files in a directory on the host."""
        if self.is_local:
            try:
                return list(entry.name for entry in path.iterdir())
            except (FileNotFoundError, OSError):
                return []
        result = self.execute_idempotent_command(f"ls -1 '{str(path)}' 2>/dev/null")
        if result.success and result.stdout.strip():
            return result.stdout.strip().split("\n")
        return []

    def _remove_directory(self, path: Path) -> None:
        """Remove a directory and its contents on the host."""
        self.execute_idempotent_command(f"rm -rf '{str(path)}'")

    def _mkdir(self, path: Path) -> None:
        """Create a directory on the host."""
        self.execute_idempotent_command(f"mkdir -p '{str(path)}'")

    def _mkdirs(self, paths: Sequence[Path]) -> None:
        """Create multiple directories on the host."""
        joined_dirs = " ".join(f"'{str(p)}'" for p in paths)
        self.execute_idempotent_command(f"mkdir -p {joined_dirs}")

    # get_ssh_connection_info is inherited from OuterHost.

    # =========================================================================
    # Outer Host Access
    # =========================================================================

    @contextmanager
    def outer_host(self) -> Iterator["OuterHostInterface | None"]:
        """Open the outer host (the underlying machine that hosts this container/sandbox).

        Delegates to ``self.provider_instance.outer_host_for(self.id)``. The
        SSH connection (when applicable) is opened on ``__enter__`` and closed
        on ``__exit__``.
        """
        with self.provider_instance.outer_host_for(self.id) as outer:
            yield outer

    # =========================================================================
    # Activity Times
    # =========================================================================

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        """Get the last reported activity time for the given type."""
        activity_path = self.host_dir / "activity" / activity_type.value.lower()
        return self._get_file_mtime(activity_path)

    def record_activity(self, activity_type: ActivitySource) -> None:
        """Record activity by writing JSON with timestamp and metadata.

        Only BOOT is valid for host-level activity.

        The JSON contains:
        - time: milliseconds since Unix epoch (int)
        - host_id: the host's ID (for debugging)

        Note: The authoritative activity time is the file's mtime, not the
        JSON content. The JSON is for debugging/auditing purposes.
        """
        if activity_type != ActivitySource.BOOT:
            raise InvalidActivityTypeError(f"Only BOOT activity can be recorded on host, got: {activity_type}")

        activity_path = self.host_dir / "activity" / activity_type.value.lower()
        now = datetime.now(timezone.utc)
        data = {
            "time": int(now.timestamp() * 1000),
            "host_id": str(self.id),
        }
        self.write_text_file(activity_path, json.dumps(data, indent=2))
        logger.trace("Recorded {} activity on host {}", activity_type, self.id)

    def get_reported_activity_content(self, activity_type: ActivitySource) -> str | None:
        """Get the content of the activity file."""
        activity_path = self.host_dir / "activity" / activity_type.value.lower()
        try:
            return self.read_text_file(activity_path)
        except FileNotFoundError:
            return None

    # =========================================================================
    # Cooperative Locking
    # =========================================================================

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = _DEFAULT_HOST_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Hold the host's exclusive, cross-actor lock for the duration of the block.

        Holds a real ``flock(2)`` on the ``host_lock`` file -- directly on local
        hosts, and over a long-lived SSH exec channel on remote hosts -- so that
        a holder running *locally* inside the host and a holder running *remotely*
        over SSH mutually exclude. Because the in-host idle-shutdown watcher tests
        the same flock, holding the lock also suppresses idle shutdown while we
        operate on the host. The lock auto-releases when the block exits (even on
        error): on remote hosts the SSH channel closes, the remote shell exits,
        the fd closes, and the lock releases.

        ``timeout_seconds=None`` blocks indefinitely until the lock is acquired
        (used by ``create`` and ``start``); a finite value raises
        ``LockNotHeldError`` if the lock cannot be acquired in time.

        On error, if ``MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1``,
        a detached on-host process re-holds the lock so the failed (remote) host
        stays up for debugging instead of idle-shutting-down.
        """
        lock_file_path = self.host_dir / _HOST_LOCK_FILENAME
        if self.is_local:
            with self._hold_local_host_lock(lock_file_path, timeout_seconds):
                yield
        else:
            with self._hold_remote_host_lock(lock_file_path, timeout_seconds):
                yield

    @contextmanager
    def _hold_local_host_lock(self, lock_file_path: Path, timeout_seconds: float | None) -> Iterator[None]:
        """Hold a real flock(2) on the local filesystem for the duration of the block."""
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(str(lock_file_path), "w")
        try:
            with log_span("acquiring host lock at {}", lock_file_path):
                if _try_acquire_flock(lock_file):
                    pass
                elif timeout_seconds is None:
                    # Contended: log once, then block indefinitely.
                    logger.info("Waiting to acquire host lock (another operation is using this host)...")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:
                    logger.info("Waiting to acquire host lock (another operation is using this host)...")
                    try:
                        wait_for(
                            lambda: _try_acquire_flock(lock_file),
                            timeout=timeout_seconds,
                            poll_interval=0.1,
                            error_message=f"Failed to acquire lock within {timeout_seconds}s",
                        )
                    except TimeoutError as e:
                        raise LockNotHeldError(str(e)) from e
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
            logger.trace("Released host lock")

    @contextmanager
    def _hold_remote_host_lock(self, lock_file_path: Path, timeout_seconds: float | None) -> Iterator[None]:
        """Hold a real flock(2) on the host over SSH for the duration of the block.

        A long-lived exec channel opens the lock fd, waits for the lock, and then
        waits for stdin EOF. Closing the channel on exit sends that EOF, so the
        remote shell exits, the fd closes, and the lock releases.
        """
        # Establish the SSH connection before grabbing the transport: it is opened
        # lazily, and the lock can be the first thing to touch it (so the transport
        # would otherwise be None). Mirrors every other transport user.
        self._ensure_connected()
        transport = self._get_paramiko_transport()
        channel = transport.open_session()
        is_lock_acquired = False
        try:
            with log_span("acquiring host lock at {} (over SSH)", lock_file_path):
                channel.exec_command(_build_remote_lock_command(lock_file_path, timeout_seconds))
                _wait_for_remote_lock_acquired(channel)
            is_lock_acquired = True
            yield
        except BaseException:
            # If an operation that held the lock fails, optionally keep the host
            # alive for debugging: launch a detached holder that re-grabs the flock
            # after our channel releases it (it blocks on flock until we release).
            if is_lock_acquired and _is_retain_lock_for_debug_enabled():
                logger.debug(
                    "Launching detached host-lock holder for debugging "
                    "(MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1)"
                )
                self._launch_detached_lock_holder(lock_file_path)
            raise
        finally:
            try:
                channel.shutdown_write()
            except (OSError, EOFError) as shutdown_error:
                # Best-effort EOF to release the remote flock; the channel.close()
                # below tears it down regardless. Log so a wedged teardown is visible.
                logger.debug("Failed to send EOF when releasing remote host lock: {}", shutdown_error)
            channel.close()
            logger.trace("Released host lock (over SSH)")

    def _launch_detached_lock_holder(self, lock_file_path: Path) -> None:
        """Launch a detached on-host process that holds the host-lock flock indefinitely.

        Used only when the debug retain flag is set: it keeps a failed remote host
        from idle-shutting-down by holding the same flock our channel is about to
        release. It blocks on flock until our channel releases, then holds forever
        (bounded only by the host's hard max-age timeout or a manual destroy).
        """
        quoted_path = shlex.quote(str(lock_file_path))
        quoted_dir = shlex.quote(str(lock_file_path.parent))
        launched_marker = shlex.quote(_LOCK_HOLDER_LAUNCHED_MARKER)
        # setsid detaches into a new session so the holder survives the channel
        # close; redirecting all stdio lets the parent shell exit immediately.
        command = (
            f"mkdir -p {quoted_dir} && "
            f"setsid sh -c 'exec 9>{quoted_path}; flock 9; exec sleep infinity' "
            f"</dev/null >/dev/null 2>&1 & "
            f"printf '%s\\n' {launched_marker}"
        )
        self._ensure_connected()
        transport = self._get_paramiko_transport()
        channel = transport.open_session()
        try:
            channel.exec_command(command)
            # Wait for the launch confirmation so the holder forks before we release.
            marker_bytes = _LOCK_HOLDER_LAUNCHED_MARKER.encode()
            buffer = b""
            while marker_bytes not in buffer:
                chunk = channel.recv(4096)
                if not chunk:
                    logger.warning("Detached host-lock holder did not confirm launch")
                    break
                buffer += chunk
        finally:
            channel.close()

    def get_reported_lock_time(self) -> datetime | None:
        """Get the mtime of the lock file (set by the truncating open at acquire)."""
        lock_path = self.host_dir / _HOST_LOCK_FILENAME
        return self._get_file_mtime(lock_path)

    def is_lock_held(self) -> bool:
        """Check whether the host lock is currently held via a non-blocking flock probe.

        The lock file persists after release (its inode must stay stable across
        local and remote holders), so file existence alone is insufficient: a
        non-blocking flock probe is the only way to tell whether it is held.
        """
        lock_path = self.host_dir / _HOST_LOCK_FILENAME

        if self.is_local:
            if not lock_path.exists():
                return False
            try:
                with open(str(lock_path), "r") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
        else:
            return self._is_remote_lock_held(lock_path)

    def _is_remote_lock_held(self, lock_path: Path) -> bool:
        """Probe whether the remote host lock is held via a single non-blocking flock test."""
        quoted_path = shlex.quote(str(lock_path))
        # Guard on existence so the probe never creates the lock file when absent.
        command = (
            f"if [ ! -e {quoted_path} ]; then echo NOT_HELD; "
            f"elif flock -n {quoted_path} -c true 2>/dev/null; then echo NOT_HELD; "
            f"else echo HELD; fi"
        )
        result = self.execute_idempotent_command(command)
        return result.success and "HELD" in result.stdout and "NOT_HELD" not in result.stdout

    # =========================================================================
    # Certified Data
    # =========================================================================

    def get_certified_data(self) -> CertifiedHostData:
        """Get all certified data from data.json."""
        data_path = self.host_dir / "data.json"
        try:
            content = self.read_text_file(data_path)
            data = json.loads(content)
            return CertifiedHostData(**data)
        except FileNotFoundError:
            now = datetime.now(timezone.utc)
            # FIXME: this is suss--we should probably just explode if data.json is missing
            #  It just means that the host is not yet properly initialized
            #  For hosts that are currently being created, that's fine, but otherwise this should count as a busted host
            #  Annoyingly we'll need to understand the difference (by checking to see if, eg, this host is locked)
            return CertifiedHostData(
                host_id=str(self.id),
                host_name=str(self.host_name),
                created_at=now,
                updated_at=now,
            )
        except ValidationError as e:
            raise HostDataSchemaError(str(data_path), str(e)) from e

    def set_certified_data(self, data: CertifiedHostData) -> None:
        """Save certified data to data.json and notify the provider."""
        with self.mngr_ctx.concurrency_group.make_concurrency_group("set_certified_data") as concurrency_group:
            # Always stamp updated_at with the current time when writing
            stamped_data = data.model_copy_update(
                to_update(data.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            data_path = self.host_dir / "data.json"
            serialized_data = json.dumps(stamped_data.model_dump(by_alias=True, mode="json"), indent=2)
            direct_write_thread = concurrency_group.start_new_thread(
                # must write atomically, otherwise we can get in trouble
                self.write_file,
                kwargs=dict(path=data_path, content=serialized_data.encode("utf-8"), mode=None, is_atomic=True),
                name="write_certified_data",
            )
            # Notify the provider so it can update any external storage (e.g., Modal volume)
            if self.on_updated_host_data:
                self.on_updated_host_data(self.id, stamped_data)
            # we're only doing this in parallel as a minor optimization--both the atomic write and the on_updated_host_data calls takes a meaningful amount of time
            direct_write_thread.join(60.0)

    def _add_generated_work_dir(self, work_dir: Path) -> None:
        """Add a work directory to the list of generated work directories."""
        certified_data = self.get_certified_data()
        existing_dirs = set(certified_data.generated_work_dirs)
        existing_dirs.add(str(work_dir))
        updated_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().generated_work_dirs, tuple(sorted(existing_dirs))),
        )
        self.set_certified_data(updated_data)

    def _remove_generated_work_dir(self, work_dir: Path) -> None:
        """Remove a work directory from the list of generated work directories."""
        certified_data = self.get_certified_data()
        existing_dirs = set(certified_data.generated_work_dirs)
        existing_dirs.discard(str(work_dir))
        updated_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().generated_work_dirs, tuple(sorted(existing_dirs))),
        )
        self.set_certified_data(updated_data)

    def _is_generated_work_dir(self, work_dir: Path) -> bool:
        """Check if a work directory was generated by mngr."""
        certified_data = self.get_certified_data()
        return str(work_dir) in certified_data.generated_work_dirs

    def _ensure_work_dir_exists(self, agent: AgentInterface) -> None:
        """Verify the agent's work_dir exists before starting.

        tmux's -c flag silently falls back to $HOME when the directory does not exist,
        which causes the agent to launch in the wrong place. This method detects the
        missing directory early and raises a clear error with a recovery command.
        """
        check = self.execute_idempotent_command(f"test -d {shlex.quote(str(agent.work_dir))}")
        if check.success:
            return

        branch = agent.get_created_branch_name()
        if branch is None:
            raise AgentStartError(
                str(agent.name),
                f"Work directory {agent.work_dir} does not exist and no branch is recorded",
            )

        raise AgentStartError(
            str(agent.name),
            f"Work directory {agent.work_dir} does not exist."
            f" To recreate it, run:\n"
            f"  git worktree add {shlex.quote(str(agent.work_dir))} {shlex.quote(branch)}",
        )

    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        """Set certified plugin data in data.json."""
        certified_data = self.get_certified_data()
        updated_plugin = dict(certified_data.plugin)
        updated_plugin[plugin_name] = data

        updated_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().plugin, updated_plugin),
        )
        self.set_certified_data(updated_data)

    def to_offline_host(self) -> HostInterface:
        return self.provider_instance.to_offline_host(self.id)

    # =========================================================================
    # Reported Plugin Data
    # =========================================================================

    def get_reported_plugin_state_file_data(self, plugin_name: str, filename: str) -> str:
        """Get a reported plugin state file."""
        plugin_path = self.host_dir / "plugin" / plugin_name / filename
        return self.read_text_file(plugin_path)

    def set_reported_plugin_state_file_data(
        self,
        plugin_name: str,
        filename: str,
        data: str,
    ) -> None:
        """Set a reported plugin state file."""
        plugin_path = self.host_dir / "plugin" / plugin_name / filename
        self.write_text_file(plugin_path, data)

    def get_reported_plugin_state_files(self, plugin_name: str) -> list[str]:
        """List all plugin state files."""
        plugin_dir = self.host_dir / "plugin" / plugin_name
        if not self._is_directory(plugin_dir):
            return []
        return self._list_directory(plugin_dir)

    # =========================================================================
    # Environment
    # =========================================================================

    def get_host_env_path(self) -> Path:
        """Get the path to the host env file."""
        return self.host_dir / "env"

    def get_env_vars(self) -> dict[str, str]:
        """Get all environment variables from the host env file."""
        env_path = self.host_dir / "env"
        try:
            content = self.read_text_file(env_path)
            return parse_env_file(content)
        except FileNotFoundError:
            return {}

    def set_env_vars(self, env: Mapping[str, str]) -> None:
        """Set all environment variables in the host env file."""
        env_path = self.host_dir / "env"
        content = _format_env_file(env)
        self.write_text_file(env_path, content)

    def get_env_var(self, key: str) -> str | None:
        """Get a single environment variable."""
        env_vars = self.get_env_vars()
        return env_vars.get(key)

    def set_env_var(self, key: str, value: str) -> None:
        """Set a single environment variable."""
        env_vars = self.get_env_vars()
        env_vars[key] = value
        self.set_env_vars(env_vars)

    # =========================================================================
    # Provider-Derived Information
    # =========================================================================

    def get_seconds_since_stopped(self) -> float | None:
        """Return the number of seconds since this host was stopped (or None if it is running)."""
        return None

    def get_stop_time(self) -> datetime | None:
        """Return the host last stop time as a datetime, or None if unknown."""
        return None

    def get_uptime_seconds(self) -> float:
        """Get host uptime in seconds."""
        # Single command that detects the platform on the host and dispatches accordingly,
        # so it works for both local and remote hosts regardless of OS
        result = self.execute_idempotent_command(
            'if [ "$(uname -s)" = "Darwin" ]; then '
            "sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,=]+' '{for(i=1;i<=NF;i++) if($i==\"sec\") print $(i+1)}' && date +%s; "
            "else "
            "cat /proc/uptime 2>/dev/null; "
            "fi"
        )
        if result.success:
            return _parse_uptime_output(result.stdout)

        return 0.0

    def get_boot_time(self) -> datetime | None:
        """Get the host boot time as a datetime.

        Returns the actual boot time from the OS, not computed from uptime,
        to avoid timing inconsistencies.
        """
        # Single command that detects the platform on the host and dispatches accordingly,
        # so it works for both local and remote hosts regardless of OS
        result = self.execute_idempotent_command(
            'if [ "$(uname -s)" = "Darwin" ]; then '
            "sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,=]+' '{for(i=1;i<=NF;i++) if($i==\"sec\") print $(i+1)}'; "
            "else "
            "grep '^btime ' /proc/stat 2>/dev/null | awk '{print $2}'; "
            "fi"
        )
        if result.success:
            return _parse_boot_time_output(result.stdout)

        return None

    def get_provider_resources(self) -> HostResources:
        """Get resources from the provider."""
        return self.provider_instance.get_host_resources(self)

    def get_outer_ssh_port(self) -> int | None:
        """Delegate to the provider, which knows whether this host has a distinct outer sshd port."""
        return self.provider_instance.get_outer_ssh_port(self.id)

    def get_ssh_host_public_keys(self) -> tuple[str | None, str | None]:
        """Delegate to the provider, which knows the host's baked sshd host public keys."""
        return self.provider_instance.get_ssh_host_public_keys(self.id)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Set tags via the provider and sync to certified data."""
        self.provider_instance.set_host_tags(self, tags)
        certified_data = self.get_certified_data()
        self.set_certified_data(
            certified_data.model_copy_update(
                to_update(certified_data.field_ref().user_tags, dict(tags)),
            )
        )
        logger.trace("Set {} tag(s) on host {}", len(tags), self.id)

    def add_tags(self, tags: Mapping[str, str]) -> None:
        """Add tags via the provider and sync to certified data."""
        self.provider_instance.add_tags_to_host(self, tags)
        certified_data = self.get_certified_data()
        merged_tags = {**certified_data.user_tags, **tags}
        self.set_certified_data(
            certified_data.model_copy_update(
                to_update(certified_data.field_ref().user_tags, merged_tags),
            )
        )

    def remove_tags(self, keys: Sequence[str]) -> None:
        """Remove tags by key via the provider and sync to certified data."""
        self.provider_instance.remove_tags_from_host(self, keys)
        certified_data = self.get_certified_data()
        keys_to_remove = set(keys)
        filtered_tags = {k: v for k, v in certified_data.user_tags.items() if k not in keys_to_remove}
        self.set_certified_data(
            certified_data.model_copy_update(
                to_update(certified_data.field_ref().user_tags, filtered_tags),
            )
        )

    # =========================================================================
    # Agent Information
    # =========================================================================

    def save_agent_data(self, agent_id: AgentId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to external storage via the provider."""
        self.provider_instance.persist_agent_data(self.id, agent_data)

    def get_agents(self) -> list[AgentInterface]:
        """Get all agents on this host."""
        agents_dir = get_agents_root_dir(self.host_dir)
        if not self._is_directory(agents_dir):
            logger.trace("Failed to find agents directory for host {}", self.id)
            return []

        agents: list[AgentInterface] = []
        for agent_id_str in self._list_directory(agents_dir):
            agent_dir = agents_dir / agent_id_str
            if self._is_directory(agent_dir):
                agent = self._load_agent_from_dir(agent_dir)
                if agent is not None:
                    agents.append(agent)
        logger.trace("Loaded {} agent(s) from host {}", len(agents), self.id)
        return agents

    def discover_agents(self) -> list[DiscoveredAgent]:
        """Get lightweight references to all agents on this host.

        This method reads only the data.json files for each agent, avoiding the
        overhead of fully loading agent objects. The certified_data field contains
        the full data.json contents.

        Note that we override the base method in order to read more directly from the host,
        since that data is more likely to be up-to-date.
        """
        with log_span("Loading all agents from host {}", self.id):
            agents_dir = get_agents_root_dir(self.host_dir)

            with log_span("Listing agent dir for host {}", self.id):
                try:
                    dir_listing = self._list_directory(agents_dir)
                except FileNotFoundError:
                    logger.trace("Failed to find agents directory for host {}", self.id)
                    return []

            with log_span("Listing agent files from dir for host {}", self.id):
                agent_refs: list[DiscoveredAgent] = []
                for dir_name in dir_listing:
                    agent_dir = agents_dir / dir_name
                    data_path = agent_dir / "data.json"
                    try:
                        content = self.read_text_file(data_path)
                    except FileNotFoundError:
                        if not self._is_directory(agent_dir):
                            logger.warning("Could not load agent reference from {}", data_path)
                        continue
                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Could not load agent reference from {} because json was invalid: {}", data_path, e
                        )
                        continue
                    ref = self._validate_and_create_discovered_agent(data)
                    if ref is not None:
                        agent_refs.append(ref)

            logger.trace("Loaded {} agent reference(s) from host {}", len(agent_refs), self.id)
            return agent_refs

    def _load_agent_from_dir(self, agent_dir: Path) -> AgentInterface | None:
        """Load an agent from its state directory.

        If the agent's stored type is no longer registered (e.g. the plugin
        was uninstalled or the type was renamed since the agent was created),
        we degrade to the orphan-fallback class wired via
        ``set_orphan_agent_class`` (configured by ``load_agents_from_plugins``
        in the agents layer) plus a base ``AgentTypeConfig``, with a logged
        warning so commands like ``mngr destroy`` / ``mngr list`` /
        ``mngr cleanup`` can still operate on the agent. If no orphan
        fallback has been wired (e.g. tests that skipped plugin loading),
        the original ``UnknownAgentTypeError`` is propagated so the missing
        setup surfaces instead of silently being swallowed.
        ``check_agent_type_known`` separately marks the agent's lifecycle
        state as ``RUNNING_UNKNOWN_AGENT_TYPE`` so users see that something
        is off.
        """
        data_path = agent_dir / "data.json"
        try:
            content = self.read_text_file(data_path)
        except FileNotFoundError:
            logger.trace("Failed to find agent data file at {}", data_path)
            return None

        data = json.loads(content)
        logger.trace("Loaded agent {} from {}", data.get("name"), agent_dir)

        agent_type = AgentTypeName(data["type"])
        try:
            resolved = resolve_agent_type(agent_type, self.mngr_ctx.config)
            resolved_class = resolved.agent_class
            resolved_config = resolved.agent_config
        except UnknownAgentTypeError:
            orphan_class = get_orphan_agent_class()
            if orphan_class is None:
                # No fallback configured (e.g. tests that didn't load the
                # agent registry). Re-raise so the test surfaces the
                # missing setup rather than silently swallowing the error.
                raise
            logger.warning(
                "Agent {} has type '{}' which is no longer registered; "
                "loading with fallback class {} so existing commands keep working.",
                data.get("name"),
                agent_type,
                orphan_class.__name__,
            )
            resolved_class = orphan_class
            resolved_config = AgentTypeConfig()

        return resolved_class(
            id=AgentId(data["id"]),
            name=AgentName(data["name"]),
            agent_type=agent_type,
            work_dir=Path(data["work_dir"]),
            create_time=datetime.fromisoformat(data["create_time"]),
            host_id=self.id,
            host=self,
            mngr_ctx=self.mngr_ctx,
            agent_config=resolved_config,
        )

    def create_agent_work_dir(
        self,
        host: OnlineHostInterface,
        path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Create the work_dir directory for a new agent."""
        transfer_mode = options.transfer_mode
        with log_span("Creating agent work directory", transfer_mode=str(transfer_mode)):
            match transfer_mode:
                case TransferMode.NONE:
                    return self._create_work_dir_in_place(host, path, options)
                case TransferMode.RSYNC:
                    return self._create_work_dir_via_rsync(host, path, options)
                case TransferMode.GIT_MIRROR:
                    return self._create_work_dir_via_git_mirror(host, path, options)
                case TransferMode.GIT_WORKTREE:
                    return self._create_work_dir_as_git_worktree(host, path, options)
                case _ as unreachable:
                    assert_never(unreachable)

    def _create_work_dir_in_place(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Use the source directory directly as the work_dir (no transfer).

        Does not modify generated_work_dirs. If the path was previously generated
        by mngr (e.g., as a worktree for another agent), GC already handles this
        correctly: it only deletes directories that are in generated_work_dirs
        AND have no living agent using them as work_dir.
        """
        target_path = options.target_path or source_path
        logger.debug("Skipped file transfer: transfer mode is none (in-place)")
        return CreateWorkDirResult(path=target_path)

    def _resolve_transfer_target(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        options: CreateAgentOptions,
    ) -> Path:
        """Determine the target directory for a transfer operation.

        For RSYNC and GIT_MIRROR modes, a target directory is always generated
        if not explicitly specified. Local hosts use host_dir/copies/<id>,
        remote hosts use host_dir/projects/<id>.
        """
        if options.target_path:
            return options.target_path
        source_is_same_host = source_host.id == self.id
        subdir = "copies" if source_is_same_host else "projects"
        return self.host_dir / subdir / str(AgentId.generate())

    def _create_work_dir_via_rsync(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Create a work_dir by rsyncing files from source to target."""
        target_path = self._resolve_transfer_target(source_host, source_path, options)

        with self.mngr_ctx.concurrency_group.make_concurrency_group("_create_work_dir_via_rsync") as cg:
            self._mkdir(target_path)

            # Track generated work dir in a thread to reduce latency
            track_thread = cg.start_new_thread(self._add_generated_work_dir, (target_path,))

            # Exclude .git if git options are present (git transfer handles it separately).
            exclude_git = options.git is not None

            self._rsync_files(
                source_host,
                source_path,
                target_path,
                extra_args=options.data_options.rsync_args,
                exclude_git=exclude_git,
            )

            track_thread.join(60.0)

        return CreateWorkDirResult(path=target_path)

    def _create_work_dir_via_git_mirror(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Create a work_dir by mirroring the git repo and optionally rsyncing extra files."""
        target_path = self._resolve_transfer_target(source_host, source_path, options)

        with self.mngr_ctx.concurrency_group.make_concurrency_group("_create_work_dir_via_git_mirror") as cg:
            self._mkdir(target_path)

            # Track generated work dir in a thread to reduce latency
            track_thread = cg.start_new_thread(self._add_generated_work_dir, (target_path,))

            created_branch_name = self._transfer_git_repo(source_host, source_path, target_path, options)
            self._transfer_extra_files(source_host, source_path, target_path, options)

            # Run rsync if enabled. This is designed for adding extra files (e.g., data files not in git),
            # not for full directory sync. By default, rsync does NOT use --delete, so existing files
            # in the target won't be removed. Users can add --delete to rsync_args if they want
            # full sync behavior with file deletion.
            if options.data_options.is_rsync_enabled:
                self._rsync_files(
                    source_host,
                    source_path,
                    target_path,
                    extra_args=options.data_options.rsync_args,
                    exclude_git=True,
                )

            self._apply_work_dir_extra_paths(
                source_host, source_path, target_path, self.mngr_ctx.config.work_dir_extra_paths
            )

            track_thread.join(60.0)

        return CreateWorkDirResult(path=target_path, created_branch_name=created_branch_name)

    def _transfer_git_repo(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        options: CreateAgentOptions,
    ) -> str | None:
        """Transfer a git repository from source to target.

        Returns the name of the branch created on the target, or None if no new branch.
        """
        new_branch_name = options.git.new_branch_name if options.git else None
        if options.git and options.git.base_branch:
            base_branch_name = options.git.base_branch
        else:
            base_branch_name = (
                _git_command_stdout(source_host, "git rev-parse --abbrev-ref HEAD", source_path) or "main"
            )

        # Get git author info and origin remote URL from source repo
        git_author_name = _git_command_stdout(source_host, "git config user.name", source_path)
        git_author_email = _git_command_stdout(source_host, "git config user.email", source_path)
        origin_url = _git_command_stdout(source_host, "git remote get-url origin", source_path)

        with info_span(
            "Transferring git repository...",
            source=str(source_path),
            target=str(target_path),
            base_branch=base_branch_name,
            new_branch=new_branch_name,
        ):
            # Ensure the target directory exists, initialize a bare git repo if
            # needed, and on remote hosts add safe.directory. All in one command
            # to minimize round trips. git init --bare is idempotent on an
            # existing bare repo so we skip the existence check.
            quoted_git_dir = shlex.quote(str(target_path / ".git"))
            quoted_target = shlex.quote(str(target_path))
            init_parts = [f"mkdir -p {quoted_target}", f"git init --bare {quoted_git_dir}"]
            if not self.is_local:
                init_parts.append(f"git config --global --add safe.directory {quoted_target}")
            init_cmd = " && ".join(init_parts)
            with log_span("Ensuring git repo on target"):
                result = self.execute_idempotent_command(init_cmd)
                if not result.success:
                    raise MngrError(f"Failed to initialize git repo on target: {result.stderr}")

            self._git_push_to_target(source_host, source_path, target_path)

            with log_span("Configuring target git repo"):
                # -f / --force: the target's working tree may have
                # pre-bootstrap files (e.g. an in-image keyframe extracted
                # by a Dockerfile RUN, plus a post-extraction mutation to
                # .mngr/image_commit_hash) that would otherwise trigger
                # "Your local changes would be overwritten" / "untracked
                # files in the way" errors. The whole point of the mirror
                # push + checkout is to materialize the operator's branch
                # tip on disk, so clobbering any pre-existing state is
                # the intended behavior.
                if new_branch_name:
                    checkout_cmd = f"git checkout -f -B {shlex.quote(new_branch_name)} {shlex.quote(base_branch_name)}"
                else:
                    checkout_cmd = f"git checkout -f {shlex.quote(base_branch_name)}"
                config_commands = [
                    "git config --bool core.bare false",
                    checkout_cmd,
                ]
                if git_author_name:
                    config_commands.append(f"git config user.name {shlex.quote(git_author_name)}")
                if git_author_email:
                    config_commands.append(f"git config user.email {shlex.quote(git_author_email)}")
                if origin_url:
                    # Use set-url if origin already exists (e.g. from the
                    # in-image keyframe's .git or the bare init), otherwise
                    # add it. Written as an explicit if/else so a failure
                    # in an earlier `&&`-chained command can't trigger
                    # this clause as a `||` fallback (bash operator
                    # precedence: `A && B || C` parses as `(A && B) || C`,
                    # so if B fails, C runs unintentionally and `add`
                    # errors with "remote origin already exists" -- a
                    # confusing co-symptom of the real failure upstream).
                    quoted_origin = shlex.quote(origin_url)
                    set_or_add = (
                        f"if git remote get-url origin >/dev/null 2>&1; "
                        f"then git remote set-url origin {quoted_origin}; "
                        f"else git remote add origin {quoted_origin}; "
                        f"fi"
                    )
                    config_commands.append(set_or_add)

                # Copy .git/info/exclude from source to target. This file is not
                # transferred by the git push since it lives outside the git
                # object store. We read it here and include it in the config command.
                exclude_content = self._read_source_git_info_exclude(source_host, source_path)
                if exclude_content is not None:
                    escaped = exclude_content.replace("'", "'\"'\"'")
                    target_exclude = ".git/info/exclude"
                    config_commands.append(f"printf '%s' '{escaped}' > {shlex.quote(target_exclude)}")

                result = self.execute_idempotent_command(
                    " && ".join(config_commands),
                    cwd=target_path,
                )
                if not result.success:
                    raise MngrError(f"Failed to configure git repo on target: {result.stderr}")

        return new_branch_name

    def _read_source_git_info_exclude(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
    ) -> str | None:
        """Read .git/info/exclude content from the source repo, or None if unavailable."""
        # Resolve the git common dir on the source so this works in worktrees,
        # where .git is a file rather than a directory.
        if source_host.is_local:
            try:
                result = self.mngr_ctx.concurrency_group.run_process_to_completion(
                    ["git", "-C", str(source_path), "rev-parse", "--git-common-dir"],
                )
            except ProcessError:
                logger.trace("Could not resolve git common dir in source, skipping info/exclude transfer")
                return None
            git_common_dir = Path(result.stdout.strip())
        else:
            result = source_host.execute_idempotent_command("git rev-parse --git-common-dir", cwd=source_path)
            if not result.success:
                logger.trace("Could not resolve git common dir in source, skipping info/exclude transfer")
                return None
            git_common_dir = Path(result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = source_path / git_common_dir

        source_exclude_path = git_common_dir / "info" / "exclude"
        try:
            return source_host.read_file(source_exclude_path).decode()
        except (FileNotFoundError, NotADirectoryError):
            logger.trace("No info/exclude in source, skipping")
            return None

    def _git_mirror_pull_from_source(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        dest_git_dir: Path,
    ) -> None:
        """Mirror a remote source repo's branches and tags into a local bare repo via ssh.

        Runs locally, so the source host's ssh key and known_hosts -- which live on this
        machine -- are valid. `dest_git_dir` may already be an initialized bare repo (the
        remote->local target, which `_transfer_git_repo` created via `git init --bare`) or
        a fresh path (the remote->remote relay's temp mirror); `git init --bare` is
        idempotent, so both cases are handled. We use a mirror-style `git fetch` rather
        than `git clone --mirror` because clone refuses a non-empty destination, and the
        remote->local target always pre-exists by this point.
        """
        source_ssh_info = source_host.get_ssh_connection_info() if isinstance(source_host, Host) else None
        if source_ssh_info is None:
            raise MngrError("Cannot determine SSH connection info for remote source host")
        user, hostname, port, key_path = source_ssh_info
        source_known_hosts = get_ssh_known_hosts_file(source_host)
        git_ssh_cmd = build_ssh_transport_command(key_path, port, source_known_hosts)
        remote_url = f"ssh://{user}@{hostname}:{port}{source_path}/.git"
        try:
            self.mngr_ctx.concurrency_group.run_process_to_completion(
                ["git", "init", "--bare", str(dest_git_dir)],
            )
            self.mngr_ctx.concurrency_group.run_process_to_completion(
                [
                    "git",
                    "-C",
                    str(dest_git_dir),
                    "fetch",
                    "--prune",
                    remote_url,
                    *GIT_MIRROR_PUSH_REFSPECS,
                ],
                env={**os.environ, "GIT_SSH_COMMAND": git_ssh_cmd},
            )
        except ProcessError as e:
            raise MngrError(f"Failed to fetch from remote source: {e}") from e

    def _git_push_to_target(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
    ) -> None:
        """Push git repo from source to target, mirroring branches and tags."""
        self._warn_if_submodules_detected(source_host, source_path)
        same_machine = _is_same_machine(source_host, self)
        target_ssh_info = self.get_ssh_connection_info()

        # Cross-machine git transfer shells out to the ssh binary (via
        # GIT_SSH_COMMAND); ssh is optional, so surface a clear error if it's absent.
        if not same_machine:
            SSH.require()

        # Same-machine push uses a bare local-on-host URL with no SSH
        # transport (covers both local-laptop-to-itself and
        # remote-host-to-itself).
        if same_machine:
            git_url = str(target_path / ".git")
        elif target_ssh_info is None:
            # Remote source -> local target: pull a bare mirror straight onto
            # this machine using the source host's ssh credentials.
            with log_span("Fetching from remote source to local target"):
                self._git_mirror_pull_from_source(source_host, source_path, target_path / ".git")
                return
        elif not source_host.is_local:
            # Remote source -> remote target: a direct `git push` would run on
            # the remote source host, but the target's ssh key and known_hosts
            # live on this (orchestrator) machine, not there. So relay through a
            # local bare mirror: pull from the source with the source's
            # credentials, then push to the target with the target's, both run
            # locally where those files exist. This mirrors the remote-to-remote
            # handling used for rsync transfers.
            user, hostname, port, key_path = target_ssh_info
            target_known_hosts = get_ssh_known_hosts_file(self)
            git_ssh_cmd = build_ssh_transport_command(key_path, port, target_known_hosts)
            git_url = f"ssh://{user}@{hostname}:{port}{target_path}/.git"
            with log_span("Relaying git repo remote-to-remote via local mirror: {}", git_url):
                with tempfile.TemporaryDirectory(prefix="mngr-git-relay-") as temp_dir:
                    mirror_git_dir = Path(temp_dir) / "mirror.git"
                    self._git_mirror_pull_from_source(source_host, source_path, mirror_git_dir)
                    command_args = [
                        "git",
                        "-C",
                        str(mirror_git_dir),
                        "push",
                        "--no-verify",
                        "--force",
                        "--prune",
                        git_url,
                        *GIT_MIRROR_PUSH_REFSPECS,
                    ]
                    try:
                        self.mngr_ctx.concurrency_group.run_process_to_completion(
                            command_args,
                            env={**os.environ, "GIT_SSH_COMMAND": git_ssh_cmd, "GIT_LFS_SKIP_PUSH": "1"},
                        )
                    except ProcessError as e:
                        raise MngrError(f"Failed to push git repo to remote target: {e}") from e
            return
        else:
            user, hostname, port, key_path = target_ssh_info
            git_url = f"ssh://{user}@{hostname}:{port}{target_path}/.git"

        # Build the environment and command for a mirror-like push. We use
        # explicit refspecs instead of --mirror to avoid pushing remote-tracking
        # refs (refs/remotes/*), which cause "inconsistent aliased update"
        # errors on git 2.45+ due to symbolic refs like refs/remotes/origin/HEAD.
        # --no-verify skips hooks, since they can sometimes fail on mirror pushes.
        env: dict[str, str] = {}
        if target_ssh_info is not None and not same_machine:
            user, hostname, port, key_path = target_ssh_info
            target_known_hosts = get_ssh_known_hosts_file(self)
            git_ssh_cmd = build_ssh_transport_command(key_path, port, target_known_hosts)
            env["GIT_SSH_COMMAND"] = git_ssh_cmd

        # Don't bother pushing LFS objects - they can be transferred later as needed,
        # and without this, it can take a ridiculously long time.
        env["GIT_LFS_SKIP_PUSH"] = "1"

        # Only same-machine and local-source pushes reach here: remote-source
        # transfers (to a local or a remote target) returned above, since they
        # cannot run a direct push from the source host with credentials that
        # only exist on this machine.
        with log_span("Pushing git repo to target: {}", git_url):
            if same_machine:
                # Run the push on the shared machine via the host interface.
                refspecs = " ".join(shlex.quote(r) for r in GIT_MIRROR_PUSH_REFSPECS)
                push_cmd = f"git push --no-verify --force --prune {shlex.quote(git_url)} {refspecs}"
                result = source_host.execute_idempotent_command(push_cmd, cwd=source_path, env=env)
                if not result.success:
                    output = (result.stderr + "\n" + result.stdout).strip()
                    raise MngrError(f"Failed to push git repo on same host: {output}")
            else:
                assert source_host.is_local, "remote-source pushes must be handled before this point"
                command_args = [
                    "git",
                    "-C",
                    str(source_path),
                    "push",
                    "--no-verify",
                    "--force",
                    "--prune",
                    git_url,
                    *GIT_MIRROR_PUSH_REFSPECS,
                ]
                try:
                    self.mngr_ctx.concurrency_group.run_process_to_completion(
                        command_args,
                        env={**os.environ, **env},
                    )
                except ProcessError as e:
                    raise MngrError(f"Failed to push git repo: {e}") from e
                logger.trace("Ran git mirror push from local source to target: {}", " ".join(command_args))

    def _warn_if_submodules_detected(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
    ) -> None:
        """Warn the user if git submodules are detected in the source repo."""
        try:
            if source_host.is_local:
                result_obj = self.mngr_ctx.concurrency_group.run_process_to_completion(
                    ["git", "submodule", "status"],
                    cwd=source_path,
                    timeout=10,
                )
                submodule_output = result_obj.stdout.strip()
            else:
                result = source_host.execute_idempotent_command(
                    "git submodule status", cwd=source_path, timeout_seconds=10
                )
                submodule_output = result.stdout.strip() if result.success else ""
        except (ProcessError, Exception):
            # If we can't check for submodules, just skip the warning
            return

        if submodule_output:
            logger.warning(
                "Detected git submodules in source repository. "
                "Submodules are not supported and will not be transferred correctly."
            )

    def _transfer_extra_files(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        options: CreateAgentOptions,
    ) -> None:
        """Transfer extra files that aren't in git (untracked, modified, gitignored)."""
        files_to_include: list[str] = []

        is_include_unclean = options.git.is_include_unclean if options.git else True
        if is_include_unclean:
            if source_host.is_local:
                result = self.mngr_ctx.concurrency_group.run_process_to_completion(
                    ["git", "-C", str(source_path), "status", "--porcelain"],
                )
                for line in result.stdout.split("\n"):
                    if line:
                        files_to_include.extend(_parse_porcelain_line(line))
            else:
                result = source_host.execute_idempotent_command("git status --porcelain", cwd=source_path)
                if result.success:
                    for line in result.stdout.split("\n"):
                        if line:
                            files_to_include.extend(_parse_porcelain_line(line))

        is_include_gitignored = options.git.is_include_gitignored if options.git else False
        if is_include_gitignored:
            if source_host.is_local:
                result = self.mngr_ctx.concurrency_group.run_process_to_completion(
                    ["git", "-C", str(source_path), "ls-files", "--others", "--ignored", "--exclude-standard"],
                )
                for line in result.stdout.split("\n"):
                    if line:
                        files_to_include.append(line)
            else:
                result = source_host.execute_idempotent_command(
                    "git ls-files --others --ignored --exclude-standard",
                    cwd=source_path,
                )
                if result.success:
                    for line in result.stdout.split("\n"):
                        if line:
                            files_to_include.append(line)

        files_to_include = list(set(files_to_include))

        if not files_to_include:
            logger.debug("Skipped extra file transfer: no files to transfer")
            return

        with log_span("Transferring extra files", count=len(files_to_include)):
            self._rsync_paths(source_host, source_path, target_path, files_to_include, exclude_git=True)

    def _rsync_paths(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        paths: list[str],
        *,
        exclude_git: bool = False,
    ) -> None:
        """Rsync specific paths from source to target using a files-from list."""
        if not paths:
            return
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            files_from_path = Path(f.name)
            for file_path in paths:
                f.write(file_path + "\n")
        try:
            self._rsync_files(
                source_host, source_path, target_path, files_from=files_from_path, exclude_git=exclude_git
            )
        finally:
            files_from_path.unlink(missing_ok=True)

    def _apply_work_dir_extra_paths(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        work_dir_path: Path,
        extra_paths: dict[str, WorkDirExtraPathMode],
    ) -> None:
        """Apply work_dir_extra_paths config: symlink or copy paths into the work directory.

        Batches all remote operations to minimize SSH round trips:
        1. One command on source_host to check which paths exist
        2. One command on self to create all symlinks (SHARE mode, same host)
        3. One rsync call for all copy paths (COPY mode, or SHARE on different host)
        """
        same_host = source_host.id == self.id

        # Validate all paths first (pure string ops, no remote calls)
        validated: list[tuple[str, WorkDirExtraPathMode]] = []
        for rel_path_str, mode in extra_paths.items():
            normalized = os.path.normpath(rel_path_str)
            if os.path.isabs(normalized):
                raise UserInputError(f"work_dir_extra_paths: absolute paths are not allowed: {rel_path_str}")
            if normalized.startswith(".."):
                raise UserInputError(f"work_dir_extra_paths: path escapes project root: {rel_path_str}")
            validated.append((rel_path_str, mode))

        if not validated:
            return

        # Batch source-exists check: one command tests all paths, outputs those that exist
        check_parts = []
        for rel_path_str, _ in validated:
            source_abs = source_path / rel_path_str
            quoted = shlex.quote(str(source_abs))
            check_parts.append(
                f"if [ -e {quoted} ] || [ -L {quoted} ]; then printf '%s\\n' {shlex.quote(rel_path_str)}; fi"
            )
        result = source_host.execute_idempotent_command("; ".join(check_parts))
        if not result.success:
            logger.warning(
                "work_dir_extra_paths: failed to check source paths (stderr: {}), skipping all extra paths",
                result.stderr.strip(),
            )
            return
        existing_paths = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

        # Route each existing path to symlink or rsync
        rsync_paths: list[str] = []
        symlink_pairs: list[tuple[str, str]] = []
        for rel_path_str, mode in validated:
            if rel_path_str not in existing_paths:
                logger.warning(
                    "work_dir_extra_paths: source path does not exist, skipping: {}", source_path / rel_path_str
                )
                continue
            if mode == WorkDirExtraPathMode.SHARE and same_host:
                symlink_pairs.append((str(source_path / rel_path_str), str(work_dir_path / rel_path_str)))
            else:
                rsync_paths.append(rel_path_str)

        # Batch symlink creation: one command handles all symlinks.
        # Errors are written directly to stderr (not accumulated in a variable)
        # because POSIX $() strips trailing newlines, which would merge messages.
        if symlink_pairs:
            script_parts = ["had_errors=0"]
            for source_str, target_str in symlink_pairs:
                s = shlex.quote(source_str)
                t = shlex.quote(target_str)
                t_parent = shlex.quote(str(Path(target_str).parent))
                script_parts.append(
                    f"if [ -e {t} ] && [ ! -L {t} ]; then "
                    f"printf 'CONFLICT: %s\\n' {t} >&2; had_errors=1; "
                    f"elif [ ! -L {t} ] || "
                    f'[ "$(readlink -f {t} 2>/dev/null)" != "$(readlink -f {s} 2>/dev/null)" ]; then '
                    f"mkdir -p {t_parent} && ln -snf {s} {t} "
                    f"|| {{ printf 'FAILED: %s\\n' {t} >&2; had_errors=1; }}; "
                    f"fi"
                )
            script_parts.append('[ "$had_errors" = 0 ]')
            result = self.execute_idempotent_command("; ".join(script_parts))
            if not result.success:
                stderr_lines = result.stderr.strip().split("\n")
                conflicts = [line.removeprefix("CONFLICT: ") for line in stderr_lines if line.startswith("CONFLICT: ")]
                failures = [line.removeprefix("FAILED: ") for line in stderr_lines if line.startswith("FAILED: ")]
                if conflicts:
                    msg = "work_dir_extra_paths: target already exists and is not a symlink: " + ", ".join(conflicts)
                    if failures:
                        msg += "; also failed to create symlinks for: " + ", ".join(failures)
                    raise UserInputError(msg)
                raise MngrError(f"work_dir_extra_paths: failed to create symlinks: {result.stderr}")

        # Rsync all copy paths in a single batch
        if rsync_paths:
            with log_span("Copying work_dir_extra_paths", count=len(rsync_paths)):
                self._rsync_paths(source_host, source_path, work_dir_path, rsync_paths)

    def copy_directory(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        """Copy a directory from source_host:source_path to self:target_path using rsync."""
        # Ensure the target directory exists -- rsync does not create intermediate parents.
        self.execute_idempotent_command(f"mkdir -p {shlex.quote(str(target_path))}", timeout_seconds=5.0)
        self._rsync_files(
            source_host,
            source_path,
            target_path,
            extra_args=extra_args,
            exclude_git=exclude_git,
        )

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        """Copy a local-machine directory to self:target_path. See OnlineHostInterface."""
        # rsync does not create intermediate parents for the destination root.
        self.execute_idempotent_command(f"mkdir -p {shlex.quote(str(target_path))}", timeout_seconds=5.0)
        # --keep-dirlinks (-K) is essential: on some hosts a destination directory is a
        # symlink to real storage (e.g. on Modal, host_dir /mngr is a symlink into the
        # mounted volume). Without -K, when the source has a real directory where the
        # receiver has a symlink-to-directory, rsync DELETES the symlink and replaces it
        # with a real directory on the ephemeral filesystem -- stranding everything that
        # lived behind the symlink (e.g. agents/<id>/data.json on the volume) and writing
        # our files to a non-persistent location. -K makes rsync follow the symlinked dir
        # and write through it instead. See github issue 1825.
        rsync_args = ["rsync", "-rlpt", "--keep-dirlinks"]
        if extra_args:
            rsync_args.extend(shlex.split(extra_args))
        source_str = str(source_path).rstrip("/") + "/"
        target_str = str(target_path).rstrip("/") + "/"

        if self.is_local:
            rsync_args.extend([source_str, target_str])
            rsync_cmd = " ".join(shlex.quote(a) for a in rsync_args)
            with log_span("rsync: local dir {} -> {}", source_path, target_path):
                result = self.execute_idempotent_command(rsync_cmd)
                if not result.success:
                    raise MngrError(f"rsync failed (local): {result.stderr}")
            return

        ssh_info = self.get_ssh_connection_info()
        assert ssh_info is not None
        user, hostname, port, key_path = ssh_info
        known_hosts = get_ssh_known_hosts_file(self)
        rsync_args.extend(["-e", build_ssh_transport_command(key_path, port, known_hosts)])
        rsync_args.extend([source_str, f"{user}@{hostname}:{target_str}"])
        with log_span("rsync: local dir -> {}@{}:{}", user, hostname, port):
            try:
                self.mngr_ctx.concurrency_group.run_process_to_completion(rsync_args)
            except ProcessError as e:
                raise MngrError(f"rsync failed (push to {hostname}): {e.stderr}") from e

    def _rsync_files(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        files_from: Path | None = None,
        exclude_git: bool = False,
    ) -> None:
        """Run rsync to transfer files from source to target.

        - If source and target are on the same machine, run rsync on that
          machine between two local paths (no SSH)
        - If source is local and target is remote, push to target via SSH
        - If target is local and source is remote, pull from source via SSH
        - If source and target are different remote hosts, sync via a local
          temp directory as intermediary (pull from source, then push to target)
        """
        same_machine = _is_same_machine(source_host, self)

        # Build rsync arguments
        rsync_args = ["rsync", "-rlpt"]
        if exclude_git:
            rsync_args.extend(["--exclude", ".git"])
        if extra_args:
            rsync_args.extend(shlex.split(extra_args))

        # files_from points at a temp file on the laptop. For cross-host
        # rsync (which always runs on the laptop) we can pass that path
        # directly. For same-machine rsync we mirror it onto the host first
        # so rsync running on the host can read it.
        host_files_from: Path | None = None
        if files_from is not None:
            if same_machine:
                paths = [p for p in files_from.read_text().splitlines() if p]
                if not paths:
                    return
                host_files_from = self.host_dir / "tmp" / f"rsync-files-from-{uuid4().hex}.txt"
                # write_file creates parent directories as needed.
                self.write_file(host_files_from, ("\n".join(paths) + "\n").encode())
                rsync_args.extend(["--files-from", str(host_files_from)])
            else:
                rsync_args.extend(["--files-from", str(files_from)])

        source_path_str = str(source_path).rstrip("/") + "/"
        target_path_str = str(target_path).rstrip("/") + "/"

        # Same-machine rsync runs between two local paths on the shared
        # machine via the host interface (no SSH, no laptop intermediary).
        if same_machine:
            rsync_args.extend([source_path_str, target_path_str])
            rsync_cmd = " ".join(shlex.quote(a) for a in rsync_args)
            with log_span("rsync: same-host {} -> {}", source_path, target_path):
                try:
                    result = self.execute_idempotent_command(rsync_cmd)
                    if not result.success:
                        raise MngrError(f"rsync failed (same-host): {result.stderr}")
                    logger.trace("Ran rsync command (same-host): {}", rsync_cmd)
                finally:
                    if host_files_from is not None:
                        self.execute_idempotent_command(
                            f"rm -f {shlex.quote(str(host_files_from))}", timeout_seconds=5.0
                        )
            return

        # Every remaining branch transfers to/from a remote host and uses the ssh
        # binary as rsync's transport (-e ssh); ssh is optional, so require it here.
        SSH.require()

        if source_host.is_local and not self.is_local:
            # Local to remote
            target_ssh_info = self.get_ssh_connection_info()
            assert target_ssh_info is not None
            user, hostname, port, key_path = target_ssh_info
            target_known_hosts = get_ssh_known_hosts_file(self)
            rsync_args.extend(["-e", build_ssh_transport_command(key_path, port, target_known_hosts)])
            rsync_args.extend([source_path_str, f"{user}@{hostname}:{target_path_str}"])
            rsync_description = f"rsync: local to remote {user}@{hostname}:{port}"
        elif not source_host.is_local and self.is_local:
            # Remote to local
            source_ssh_info = source_host.get_ssh_connection_info() if isinstance(source_host, Host) else None
            assert source_ssh_info is not None
            user, hostname, port, key_path = source_ssh_info
            source_known_hosts = get_ssh_known_hosts_file(source_host)
            rsync_args.extend(["-e", build_ssh_transport_command(key_path, port, source_known_hosts)])
            rsync_args.extend([f"{user}@{hostname}:{source_path_str}", target_path_str])
            rsync_description = f"rsync: remote to local {user}@{hostname}:{port}"
        else:
            # Remote to remote: sync via local temp directory as intermediary
            source_ssh_info = source_host.get_ssh_connection_info() if isinstance(source_host, Host) else None
            assert source_ssh_info is not None
            target_ssh_info = self.get_ssh_connection_info()
            assert target_ssh_info is not None

            src_user, src_hostname, src_port, src_key_path = source_ssh_info
            tgt_user, tgt_hostname, tgt_port, tgt_key_path = target_ssh_info

            with tempfile.TemporaryDirectory(prefix="mngr-rsync-") as temp_dir:
                temp_path_str = temp_dir.rstrip("/") + "/"

                with log_span(
                    "rsync: remote-to-remote via local intermediary ({}@{}:{} -> {}@{}:{})",
                    src_user,
                    src_hostname,
                    src_port,
                    tgt_user,
                    tgt_hostname,
                    tgt_port,
                ):
                    # Step 1: pull from source remote to local temp
                    pull_args = list(rsync_args)
                    src_known_hosts = get_ssh_known_hosts_file(source_host)
                    pull_args.extend(["-e", build_ssh_transport_command(src_key_path, src_port, src_known_hosts)])
                    pull_args.extend([f"{src_user}@{src_hostname}:{source_path_str}", temp_path_str])
                    try:
                        self.mngr_ctx.concurrency_group.run_process_to_completion(pull_args)
                    except ProcessError as e:
                        raise MngrError(f"rsync failed (pull from source): {e.stderr}") from e
                    logger.trace("Ran rsync pull command: {}", " ".join(pull_args))

                    # Step 2: push from local temp to target remote
                    # Rebuild base args without files_from since the temp dir already contains only the desired files
                    push_args = ["rsync", "-rlpt"]
                    if exclude_git:
                        push_args.extend(["--exclude", ".git"])
                    if extra_args:
                        push_args.extend(shlex.split(extra_args))
                    tgt_known_hosts = get_ssh_known_hosts_file(self)
                    push_args.extend(["-e", build_ssh_transport_command(tgt_key_path, tgt_port, tgt_known_hosts)])
                    push_args.extend([temp_path_str, f"{tgt_user}@{tgt_hostname}:{target_path_str}"])
                    try:
                        self.mngr_ctx.concurrency_group.run_process_to_completion(push_args)
                    except ProcessError as e:
                        raise MngrError(f"rsync failed (push to target): {e.stderr}") from e
                    logger.trace("Ran rsync push command: {}", " ".join(push_args))

            return

        with log_span("{}", rsync_description):
            try:
                self.mngr_ctx.concurrency_group.run_process_to_completion(rsync_args)
            except ProcessError as e:
                raise MngrError(f"rsync failed: {e.stderr}") from e
            logger.trace("Ran rsync command: {}", " ".join(rsync_args))

    def _create_work_dir_as_git_worktree(
        self,
        host: OnlineHostInterface,
        source_path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Create a work_dir using git worktree.

        Worktrees are placed at <host_dir>/worktrees/<name>-<uuid>/ by default,
        or at <worktree_base_folder>/<name>-<uuid>/ if worktree_base_folder is set.

        In update mode (options.is_update), the worktree already exists.
        Instead of creating a new worktree, we update the existing one by
        checking out the desired branch.
        """
        if host.id != self.id:
            raise UserInputError("Worktree mode only works when source is on the same host")

        if options.target_path is not None:
            work_dir_path = options.target_path
        else:
            agent_name = options.name or AgentName(GENERIC_AGENT_NAME_HINT)
            work_dir_dir_name = f"{agent_name}-{uuid4().hex}"
            worktree_base = options.worktree_base_folder or (self.host_dir / "worktrees")
            work_dir_path = worktree_base / work_dir_dir_name

        new_branch_name = options.git.new_branch_name if options.git else None
        base_branch = options.git.base_branch if options.git else None

        if not new_branch_name and not base_branch:
            raise UserInputError("Worktree mode requires a branch. Use --branch BRANCH or --branch BASE:NEW.")

        branch_label = new_branch_name or base_branch

        if options.is_update:
            # Update existing worktree: checkout the desired branch
            with log_span("Updating git worktree", path=str(work_dir_path), branch=branch_label):
                git_wt = f"git -C {shlex.quote(str(work_dir_path))}"
                if new_branch_name:
                    checkout_cmd = (
                        f"{git_wt} checkout -B {shlex.quote(new_branch_name)} {shlex.quote(base_branch or 'HEAD')}"
                    )
                else:
                    checkout_cmd = f"{git_wt} checkout {shlex.quote(base_branch or 'HEAD')}"
                result = self.execute_idempotent_command(checkout_cmd)
                if not result.success:
                    raise MngrError(f"Failed to update git worktree: {result.stderr}")

                created_branch = new_branch_name

                self._apply_work_dir_extra_paths(
                    host, source_path, work_dir_path, self.mngr_ctx.config.work_dir_extra_paths
                )

                return CreateWorkDirResult(path=work_dir_path, created_branch_name=created_branch)

        with log_span("Creating git worktree", path=str(work_dir_path), branch=branch_label):
            git_c = f"git -C {shlex.quote(str(source_path))}"
            mkdir_cmd = f"mkdir -p {work_dir_path.parent}"

            # git worktree add <path> [-b <new>] [<base>]
            worktree_args = [mkdir_cmd, "&&", git_c, "worktree", "add", shlex.quote(str(work_dir_path))]
            if new_branch_name:
                worktree_args += ["-b", shlex.quote(new_branch_name)]
            if base_branch:
                worktree_args.append(shlex.quote(base_branch))
            cmd = " ".join(worktree_args)
            created_branch = new_branch_name

            result = self.execute_stateful_command(cmd)
            if not result.success:
                stderr = result.stderr or ""
                if "already checked out" in stderr or "already used by worktree" in stderr:
                    raise UserInputError(
                        f"{stderr.strip()}\n"
                        f"To create a new branch instead, use --branch BASE: or --branch BASE:new-name\n"
                        f"To work directly in the existing worktree, cd into it and re-run with --transfer=none"
                    )
                # `git worktree add` cannot resolve any commit reference in a
                # repo with no commits and reports a cryptic error. Probe HEAD
                # directly on the failure path so the empty-repo case gets a
                # clear message regardless of git's exact stderr wording.
                head_check = self.execute_idempotent_command(f"{git_c} rev-parse --verify HEAD")
                if not head_check.success:
                    raise UserInputError(
                        f"Cannot create an agent in {source_path}: the git repository has no commits. "
                        "Please make an initial commit first."
                    )
                raise MngrError(f"Failed to create git worktree: {stderr}")

            # Track generated work directories at the host level
            self._add_generated_work_dir(work_dir_path)

            # `git worktree add` only checks out the committed state of the base branch.
            # Mirror the git-mirror codepath and copy over uncommitted (and optionally
            # gitignored) files from the source so --include-unclean works in worktree mode.
            self._transfer_extra_files(host, source_path, work_dir_path, options)

            self._apply_work_dir_extra_paths(
                host, source_path, work_dir_path, self.mngr_ctx.config.work_dir_extra_paths
            )

            return CreateWorkDirResult(path=work_dir_path, created_branch_name=created_branch)

    def create_agent_state(
        self,
        work_dir_path: Path,
        options: CreateAgentOptions,
        created_branch_name: str | None = None,
    ) -> AgentInterface:
        """Create the agent state directory and return the agent.

        In update mode (options.is_update), the state directory already exists.
        We preserve the original create_time and update all other fields.
        """
        agent_id = options.agent_id if options.agent_id is not None else AgentId.generate()
        agent_name = options.name or AgentName(f"agent-{str(agent_id)}")
        agent_type = options.agent_type
        with info_span(
            "Creating agent state...",
            agent_id=str(agent_id),
            agent_name=str(agent_name),
            agent_type=str(agent_type),
        ):
            resolved = resolve_agent_type(agent_type, self.mngr_ctx.config)

            state_dir = get_agent_state_dir_path(self.host_dir, agent_id)
            # _mkdirs uses mkdir -p, which is idempotent for existing directories
            events_dir = state_dir / "events"
            services_events_dir = events_dir / "services"
            requests_events_dir = events_dir / "requests"
            self._mkdirs(
                [
                    state_dir,
                    events_dir,
                    services_events_dir,
                    requests_events_dir,
                    state_dir / "activity",
                    state_dir / "commands",
                ]
            )

            # Pre-create empty events.jsonl files so that `mngr event --follow`
            # finds the sources immediately on startup, rather than waiting for a
            # 10-second rescan after the agent's services start writing events.
            services_events_file = services_events_dir / "events.jsonl"
            requests_events_file = requests_events_dir / "events.jsonl"
            self.execute_idempotent_command(f"touch '{services_events_file}' '{requests_events_file}'")

            # In update mode, preserve the original create_time from existing data.json
            if options.is_update:
                create_time = self._read_existing_create_time(state_dir)
            else:
                create_time = datetime.now(timezone.utc)

            agent = resolved.agent_class(
                id=agent_id,
                name=agent_name,
                agent_type=agent_type,
                work_dir=work_dir_path,
                create_time=create_time,
                host_id=self.id,
                host=self,
                mngr_ctx=self.mngr_ctx,
                agent_config=resolved.agent_config,
            )

            command = agent.assemble_command(
                host=self,
                agent_args=options.agent_args,
                command_override=options.command,
                initial_message=options.initial_message,
            )
            command_str = str(command)

            data = {
                "id": str(agent_id),
                "name": str(agent_name),
                "type": str(agent_type),
                "work_dir": str(work_dir_path),
                "create_time": create_time.isoformat(),
                "command": command_str,
                "additional_commands": [
                    {"command": str(cmd.command), "window_name": cmd.window_name}
                    for cmd in options.additional_commands
                ],
                "initial_message": options.initial_message,
                "resume_message": options.resume_message,
                "ready_timeout_seconds": options.ready_timeout_seconds,
                "start_on_boot": False,
                "labels": dict(options.label_options.labels),
                "created_branch_name": created_branch_name,
                "tmux": options.tmux.to_data_dict(),
            }

            # this is really just here to parallelize some of the work and decrease latency to creating a host
            with self.mngr_ctx.concurrency_group.make_concurrency_group("write_agent_state") as concurrency_group:
                threads: list[ObservableThread] = []

                threads.append(
                    concurrency_group.start_new_thread(
                        self.write_text_file, (state_dir / "data.json", json.dumps(data, indent=2))
                    )
                )

                # Persist agent data to external storage (e.g., Modal volume)
                threads.append(
                    concurrency_group.start_new_thread(self.provider_instance.persist_agent_data, (self.id, data))
                )

                # Record CREATE activity for idle detection
                threads.append(concurrency_group.start_new_thread(agent.record_activity, (ActivitySource.CREATE,)))

                # Notify plugins that the agent state directory was created
                with log_span("Calling on_agent_state_dir_created hooks"):
                    self.mngr_ctx.pm.hook.on_agent_state_dir_created(agent=agent, host=self)

                # make sure they all finish in a reasonable amount of time
                for thread in threads:
                    thread.join(60.0)

            return agent

    def _read_existing_create_time(self, state_dir: Path) -> datetime:
        """Read the create_time from an existing agent's data.json, falling back to now."""
        data_path = state_dir / "data.json"
        try:
            content = self.read_text_file(data_path)
            data = json.loads(content)
            return datetime.fromisoformat(data["create_time"])
        except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError) as e:
            logger.warning("Could not read existing create_time from {}: {}", data_path, e)
            return datetime.now(timezone.utc)

    def _get_agent_state_dir(self, agent: AgentInterface) -> Path:
        """Get the state directory for an agent."""
        return get_agent_state_dir_path(self.host_dir, agent.id)

    def get_agent_env_path(self, agent: AgentInterface) -> Path:
        """Get the path to the agent's environment file."""
        return self._get_agent_state_dir(agent) / "env"

    def _collect_agent_env_vars(
        self,
        agent: AgentInterface,
        options: CreateAgentOptions,
    ) -> dict[str, str]:
        """Collect environment variables from options.

        Combines env vars from:
        1. MNGR-specific agent variables (id, name, state_dir, work_dir)
        2. programmatic defaults
        3. env_files (loaded in order)
        4. env_vars (explicit KEY=VALUE pairs)

        Later sources override earlier ones.

        Note: pass_env_vars is resolved at the CLI level before this is called,
        and merged into env_vars with explicit env_vars taking precedence.
        """
        env_vars: dict[str, str] = {}

        # 1. Add MNGR-specific environment variables
        agent_state_dir = self._get_agent_state_dir(agent)
        env_vars["MNGR_HOST_DIR"] = str(self.host_dir)
        env_vars["MNGR_AGENT_ID"] = str(agent.id)
        env_vars["MNGR_AGENT_NAME"] = str(agent.name)
        env_vars["MNGR_AGENT_STATE_DIR"] = str(agent_state_dir)
        env_vars["MNGR_AGENT_WORK_DIR"] = str(agent.work_dir)
        env_vars["LLM_USER_PATH"] = str(agent_state_dir / "llm_data")
        # The agent's primary tmux window name, exported so in-session helpers
        # (e.g. the ttyd attach script) can target the window by name rather than
        # the literal :0 index, keeping them independent of the user's base-index.
        env_vars["MNGR_PRIMARY_WINDOW_NAME"] = self.mngr_ctx.config.tmux.primary_window_name

        # 2. Add programmatic defaults
        base_branch = (options.git.base_branch if options.git else None) or ""
        env_vars["MNGR_GIT_BASE_BRANCH"] = base_branch
        # Also export the code-guardian-namespaced form so the plugin's stop hook
        # picks up the per-agent base branch without needing a per-worktree
        # .reviewer/settings.local.json. See https://github.com/imbue-ai/code-guardian
        env_vars["CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH"] = base_branch

        # 3. Load from env_files
        for env_file in options.environment.env_files:
            content = env_file.read_text()
            file_vars = parse_env_file(content)
            env_vars.update(file_vars)

        # 4. Add explicit env_vars
        for env_var in options.environment.env_vars:
            env_vars[env_var.key] = env_var.value

        # 5. Let the agent modify env vars (e.g. set UV_TOOL_DIR for per-agent mngr)
        agent.modify_env_vars(host=self, env_vars=env_vars)

        return env_vars

    def _write_agent_env_file(self, agent: AgentInterface, env_vars: Mapping[str, str]) -> None:
        """Write environment variables to the agent's env file."""
        if not env_vars:
            return

        env_path = self.get_agent_env_path(agent)
        content = _format_env_file(env_vars)
        self.write_text_file(env_path, content)
        logger.debug("Wrote env vars", count=len(env_vars), path=str(env_path))

    def _build_source_env_commands(self, agent: AgentInterface) -> list[str]:
        """Build shell commands that source host and agent env files."""
        host_env_path = self.host_dir / "env"
        agent_env_path = self.get_agent_env_path(agent)
        return build_source_env_shell_commands(host_env_path, agent_env_path)

    def build_source_env_prefix(self, agent: AgentInterface) -> str:
        """Build a shell prefix that sources host and agent env files if they exist."""
        commands = self._build_source_env_commands(agent)
        return " && ".join(commands) + " && "

    def provision_agent(
        self,
        agent: AgentInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Provision an agent (install packages, configure, etc.).

        Applies all provisioning in a logical order:
        Call agent.on_before_provisioning() (validation only)
        Call agent.get_provision_file_transfers() to collect file transfers
        Validate required files exist, execute file transfers
        Write environment variables to agent env file (before agent.provision()
           so agent provisioning can use env vars like UV_TOOL_DIR)
        Ensure mngr_log.sh exists at host and agent level
        Call agent.provision() (agent-type-specific provisioning)
        Create directories (so paths exist for uploads)
        Upload files (files exist before modifications)
        Run extra provision commands (user-level setup, with env vars sourced)
        Call agent.on_after_provisioning() (finalization)
        """
        # Merge agent type provisioning fields into options before any other logic.
        # Use resolve_agent_type to get the parent-merged config so that
        # provisioning fields defined on a parent type are inherited by children.
        resolved = resolve_agent_type(options.agent_type, mngr_ctx.config)
        options = _merge_agent_type_provisioning(resolved.agent_config, options)

        with self.mngr_ctx.concurrency_group.make_concurrency_group("provision_agent") as concurrency_group:
            # Call pre-provisioning validation on agent
            with log_span("Calling on_before_provisioning for agent {}", agent.name):
                agent.on_before_provisioning(host=self, options=options, mngr_ctx=mngr_ctx)

            # Collect file transfers from agent
            with log_span("Collecting file transfers for agent {}", agent.name):
                all_file_transfers = list(
                    agent.get_provision_file_transfers(host=self, options=options, mngr_ctx=mngr_ctx)
                )

            # Resolve the remote home once: bulk uploads stage files under their
            # absolute remote paths, and relative destinations resolve against it.
            # (Unused for local hosts, which write directly.) Fail loudly if the remote
            # home cannot be determined -- an empty home would silently misplace every
            # `~/...` or relative upload at the filesystem root instead of under $HOME.
            remote_home = ""
            if not self.is_local:
                home_result = self.execute_idempotent_command("echo $HOME")
                if not home_result.success:
                    raise MngrError(f"Failed to determine remote home directory: {home_result.stderr}")
                remote_home = home_result.stdout.strip()
                if not remote_home:
                    raise MngrError("Failed to determine remote home directory: $HOME resolved to an empty string")

            # Validate required files exist and execute transfers
            agent_file_transfer_thread = concurrency_group.start_new_thread(
                self._execute_agent_file_transfers, (agent, all_file_transfers, remote_home)
            )

            # Write environment variables to agent env file (before agent.provision()
            # so that agent provisioning can use env vars like UV_TOOL_DIR)
            env_vars = self._collect_agent_env_vars(agent, options)
            self._write_agent_env_file(agent, env_vars)

            # Ensure the shared shell libraries (mngr_log.sh, mngr_transcript_lib.sh)
            # exist at both host and agent level so that all bash scripts can source
            # them for logging, timestamp utilities, and raw-transcript primitives.
            ensure_shared_libs_thread = concurrency_group.start_new_thread(self._ensure_shared_shell_libs, (agent,))

            # files need to be there before provisioning--even making this a thread was just a minor optimization:
            agent_file_transfer_thread.join(60.0)

            # Call agent.provision() for agent-type-specific provisioning
            with log_span("Calling provision for agent {}", agent.name):
                agent.provision(host=self, options=options, mngr_ctx=mngr_ctx)

            provisioning = options.provisioning
            with log_span(
                "Applying user provisioning commands",
                agent_name=str(agent.name),
                dirs=len(provisioning.create_directories),
                uploads=len(provisioning.upload_files),
                extra_cmds=len(provisioning.extra_provision_commands),
            ):
                # Create directories
                for directory in provisioning.create_directories:
                    self._mkdir(directory)
                    logger.trace("Created directory: {}", directory)

                # Upload files in a single bulk transfer (rsync for remote hosts).
                # skip_missing=False: a user-specified upload whose source is missing
                # is an error.
                upload_files_in_bulk(
                    self,
                    {spec.remote_path: spec.local_path for spec in provisioning.upload_files},
                    remote_home,
                    skip_missing=False,
                )

                # Build the source prefix for commands (sources host env, then agent env)
                source_prefix = self.build_source_env_prefix(agent)

                # Run extra provision commands (with env vars sourced)
                for cmd in provisioning.extra_provision_commands:
                    result = self.execute_idempotent_command(source_prefix + cmd, cwd=agent.work_dir)
                    logger.trace("Ran extra provision command: {}", cmd)
                    if not result.success:
                        raise MngrError(f"Extra provision command failed: {cmd}\nstderr: {result.stderr}")

            # should be done by now
            ensure_shared_libs_thread.join(60.0)

            # Call post-provisioning on agent
            with log_span("Calling on_after_provisioning for agent {}", agent.name):
                agent.on_after_provisioning(host=self, options=options, mngr_ctx=mngr_ctx)

    _SHARED_SHELL_LIB_NAMES: ClassVar[tuple[str, ...]] = (
        "mngr_log.sh",
        "mngr_transcript_lib.sh",
        "mngr_common_transcript_lib.sh",
    )

    def _ensure_shared_shell_libs(self, agent: AgentInterface) -> None:
        """Write the shared shell libraries to host-level and agent-level commands dirs.

        These libraries are sourced by mngr bash scripts and must exist on both
        levels so host-level (``activity_watcher.sh``) and agent-level
        (``stream_transcript.sh``, ``chat.sh``) scripts can source them
        consistently.

        - ``mngr_log.sh`` provides shared JSONL logging and cross-platform
          timestamp utilities.
        - ``mngr_transcript_lib.sh`` provides the raw-transcript primitives
          (field extraction, id-set construction, offset reconciliation,
          bounded sed-append, percent-encoded path keys) shared by per-agent
          streamers such as claude's ``stream_transcript.sh``.
        - ``mngr_common_transcript_lib.sh`` provides the common-transcript
          converter primitives (the convert lock and the turn-end flush) shared
          by per-agent ``common_transcript.sh`` converters and the turn-end
          hooks that flush them (claude's ``wait_for_stop_hook.sh``,
          antigravity's ``statusline.sh``).
        """
        host_commands = self.host_dir / "commands"
        agent_commands = self._get_agent_state_dir(agent) / "commands"
        # These should stay per-file write_file calls (not upload_files_in_bulk) because
        # they need the executable bit (mode="0755"), which the rsync staging helper does
        # not preserve. The set is fixed and tiny (two libs x two destinations), so the
        # per-file cost is negligible and this is exempt from the bulk-upload ratchet.
        for name in self._SHARED_SHELL_LIB_NAMES:
            content_bytes = importlib.resources.files(mngr_resources).joinpath(name).read_text().encode()
            self.write_file(host_commands / name, content_bytes, mode="0755")
            self.write_file(agent_commands / name, content_bytes, mode="0755")

    def _execute_agent_file_transfers(
        self,
        agent: AgentInterface,
        transfers: list[FileTransferSpec],
        remote_home: str,
    ) -> None:
        """Validate and execute file transfers from the agent.

        First validates that all required files exist, then transfers everything in a
        single bulk upload (rsync for remote hosts). Always emits a "Transferring agent
        files" log_span (with count=0 when the agent declared no transfers) so timing is
        visible at -vv.
        """
        with log_span("Transferring agent files", count=len(transfers)):
            if not transfers:
                return

            # Validate required files first
            missing_required: list[Path] = []
            for transfer in transfers:
                if transfer.is_required and not transfer.local_path.exists():
                    missing_required.append(transfer.local_path)

            if missing_required:
                missing_str = ", ".join(str(p) for p in missing_required)
                raise MngrError(f"Required files for provisioning not found: {missing_str}")

            # Required files were validated above; skip_missing=True drops any optional
            # transfer whose source does not exist.
            uploads = {agent.work_dir / transfer.agent_path: transfer.local_path for transfer in transfers}
            upload_files_in_bulk(self, uploads, remote_home, skip_missing=True)

    def rename_agent(
        self,
        agent_ref: DiscoveredAgent,
        new_name: AgentName,
        labels_to_merge: Mapping[str, str] | None = None,
    ) -> DiscoveredAgent:
        """Rename an agent (optionally merging labels) and return the updated ref.

        The operation is idempotent: if interrupted mid-rename, re-running
        will complete it. This works because data.json (the "commit point")
        is updated last, while tmux and env changes are applied first and
        are safe to repeat.

        When ``labels_to_merge`` is non-empty, those keys are merged into the
        agent's existing labels as part of the same atomic data.json write, so
        an external observer of the agent's state never sees the rename without
        also seeing the new labels.
        """
        agent_id = agent_ref.agent_id
        with log_span(
            "Renaming agent",
            agent_id=str(agent_id),
            old_name=str(agent_ref.agent_name),
            new_name=str(new_name),
        ):
            # Prevent same-host name collisions (the tmux session name is derived
            # from the agent name, so duplicates would share a session).
            self._check_rename_conflict(agent_id, new_name)

            old_name = agent_ref.agent_name
            agent_state_dir = get_agent_state_dir_path(self.host_dir, agent_id)
            data_path = agent_state_dir / "data.json"

            # Rename the tmux session first (idempotent -- no-ops if session doesn't exist with old name)
            old_session_name = self.mngr_ctx.config.agent_session_name(old_name)
            new_session_name = self.mngr_ctx.config.agent_session_name(new_name)
            old_session_target = TmuxSessionTarget(session_name=old_session_name).as_shell_arg()
            result = self.execute_idempotent_command(
                f"tmux has-session -t {old_session_target} 2>/dev/null && "
                f"tmux rename-session -t {old_session_target} -- {shlex.quote(new_session_name)} || true"
            )
            logger.debug("Tmux rename result: success={}, stdout={}", result.success, result.stdout.strip())

            # Update the MNGR_AGENT_NAME env var in the agent's env file
            env_path = agent_state_dir / "env"
            try:
                env_content = self.read_text_file(env_path)
                updated_lines: list[str] = []
                for line in env_content.splitlines():
                    if line.startswith("MNGR_AGENT_NAME="):
                        updated_lines.append(f"MNGR_AGENT_NAME={new_name}")
                    else:
                        updated_lines.append(line)
                self.write_file(env_path, ("\n".join(updated_lines) + "\n").encode(), is_atomic=True)
            except FileNotFoundError:
                logger.debug("No env file found for agent {}, skipping env update", agent_id)

            # Update data.json last (the "commit point" for the rename). Any
            # provided labels are merged into the existing labels in the same
            # atomic write so observers see the new name and the new labels
            # together.
            content = self.read_text_file(data_path)
            data = json.loads(content)
            updated = apply_rename_to_agent_data(data, new_name, labels_to_merge)
            self.write_file(data_path, json.dumps(updated, indent=2).encode(), is_atomic=True)
            self.save_agent_data(agent_id, updated)

            return DiscoveredAgent(
                host_id=self.id,
                agent_id=agent_id,
                agent_name=new_name,
                provider_name=self.provider_instance.name,
                certified_data=updated,
            )

    def destroy_agent(self, agent: AgentInterface) -> None:
        """Destroy an agent and clean up its resources.

        Returns normally if the agent was fully destroyed or only benign "already gone"
        outcomes occurred; raises ``CleanupFailedGroup`` carrying the *real* cleanup
        failures (resources left behind) otherwise. Each step is best-effort: a failure on
        the on_destroy hook, the stop, the state-dir removal, or the persisted-data removal
        is recorded and the remaining steps still run (see specs/cleanup-error-aggregation.md).
        """
        with log_span("Destroying agent", agent_id=str(agent.id), agent_name=str(agent.name)):
            with collecting_cleanup_failures() as failures:
                # Plugin on_destroy hook (runs before teardown). A hook failure is recorded but
                # must not prevent the actual teardown below. (An unexpected non-MngrError is a
                # plugin bug and is left to propagate.)
                try:
                    agent.on_destroy(self)
                except MngrError as e:
                    logger.warning("on_destroy hook failed for agent {} on host {}: {}", agent.name, self.id, e)
                    failures.append(
                        CleanupFailure(
                            category=CleanupFailureCategory.OTHER,
                            message=f"on_destroy hook failed for agent {agent.name}: {e}",
                            agent_name=agent.name,
                            host_id=self.id,
                        )
                    )

                # Kill the agent's processes and tmux session. stop_agents raises its own
                # failures rather than returning them; absorb them into this aggregate so
                # the remaining teardown steps still run.
                try:
                    self.stop_agents([agent.id])
                except CleanupFailedGroup as group:
                    collect_cleanup_failures(failures, group)

                # Remove the agent's on-host state directory. A missing dir is benign (rm -rf
                # exits 0 with no stderr); a permission/IO error leaves local state behind.
                state_dir = get_agent_state_dir_path(self.host_dir, agent.id)
                _result, failure = self._run_classified_cleanup_command(
                    f"rm -rf '{str(state_dir)}'",
                    failure_category=CleanupFailureCategory.LOCAL_STATE_REMAINS,
                    benign_stderr_substrings=(),
                    agent_name=agent.name,
                )
                if failure is not None:
                    failures.append(failure)

                # Remove persisted agent data from external storage (e.g., Modal volume).
                try:
                    self.provider_instance.remove_persisted_agent_data(self.id, agent.id)
                except MngrError as e:
                    logger.warning(
                        "Failed to remove persisted data for agent {} on host {}: {}", agent.name, self.id, e
                    )
                    failures.append(
                        CleanupFailure(
                            category=CleanupFailureCategory.LOCAL_STATE_REMAINS,
                            message=f"failed to remove persisted data for agent {agent.name}: {e}",
                            agent_name=agent.name,
                            host_id=self.id,
                        )
                    )

    def _build_env_shell_command(self, agent: AgentInterface) -> str:
        """Build a shell command that sources env files and then execs into a shell.

        Uses MNGR_SAVED_DEFAULT_TMUX_COMMAND if set (the user's original
        default-command, saved via tmux set-environment during session creation),
        falling back to $SHELL (the user's login shell) or bash otherwise.
        """
        commands = self._build_source_env_commands(agent)
        # Note: no quotes, because the saved command may have multiple words
        commands.append("exec ${MNGR_SAVED_DEFAULT_TMUX_COMMAND:-${SHELL:-bash}}")
        return "bash -c " + shlex.quote("; ".join(commands))

    def _get_host_tmux_config_path(self) -> Path:
        """Get the path to the host's tmux config file.

        Using a host-level config instead of per-agent configs avoids issues
        where tmux key bindings (which are server-wide) would be overwritten
        by each new agent, causing Ctrl-q to destroy the wrong agent.
        """
        return self.host_dir / "tmux.conf"

    def _create_host_tmux_config(self) -> Path:
        """Create a tmux config file for the host with hotkeys for agent management.

        The config holds only mngr's own settings; tmux loads the user's
        ~/.tmux.conf itself when the server starts.

        The config:
        1. Use mngr's preferred status-left-length (tmux default is 10)
        2. Enable set-titles so the agent's title reaches the outer terminal tab
        3. Adds a Ctrl-q binding that detaches and destroys the current agent
        4. Adds a Ctrl-t binding that detaches and stops the current agent

        This uses the tmux session_name format variable in the commands,
        which expands to the current session name at runtime. This approach
        works correctly even when multiple agents share a tmux server, because
        each session's binding correctly references its own session name.

        For local hosts, the bindings directly exec into `mngr destroy`/`mngr stop`
        via `tmux detach-client -E`. For remote hosts, the bindings write a signal
        file (containing "destroy" or "stop") and detach normally. The SSH wrapper
        script checks for these signal files after tmux exits and returns an exit
        code that the local mngr process uses to run the appropriate command.

        Returns the path to the created config file.
        """
        config_path = self._get_host_tmux_config_path()

        # Build the config content
        # The session_name variable is a tmux format that gets expanded at runtime
        # Yes, it has to get passed through in this weird way
        lines = [
            "# Mngr host tmux config",
            "# Auto-generated - do not edit",
            "",
            "# Widen status-left to show more session name, i.e. '[mngr-<agent_name>]'",
            f"set -g status-left-length {_TMUX_STATUS_LEFT_LENGTH}",
            "",
            "# Forward the agent's title to the outer terminal tab (tmux default is off)",
            "set -g set-titles on",
            f'set -g set-titles-string "{_TMUX_SET_TITLES_STRING}"',
            "",
        ]

        # Source the user's extra mngr-specific tmux config if one is configured.
        # Unlike this auto-generated file, that path is never overwritten by mngr,
        # so it is a stable place for users to add their own session config. (This
        # is distinct from ~/.tmux.conf, which tmux loads itself at server start;
        # mngr does not re-source that, to avoid re-running non-idempotent user
        # config on every agent creation.) The path is interpolated unquoted so a
        # leading ~ is expanded by the host's shell and tmux; additional_config_path
        # is expected to be an absolute or ~-relative path without shell-special
        # characters.
        additional_config_path = self.mngr_ctx.config.tmux.additional_config_path
        if additional_config_path is not None:
            additional_config_str = str(additional_config_path)
            lines.extend(
                [
                    "# Source the user's extra mngr tmux config if it exists",
                    f"if-shell 'test -f {additional_config_str}' 'source-file {additional_config_str}'",
                    "",
                ]
            )

        if self.is_local:
            # Local hosts: detach and exec into mngr destroy/stop directly
            lines.extend(
                [
                    "# Ctrl-q: Detach and destroy the agent whose session this is",
                    """bind -n C-q run-shell 'SESSION=$(tmux display-message -p "#{session_name}"); tmux detach-client -E "mngr destroy --session $SESSION -f"'""",
                    "",
                    "# Ctrl-t: Detach and stop the agent whose session this is",
                    """bind -n C-t run-shell 'SESSION=$(tmux display-message -p "#{session_name}"); tmux detach-client -E "mngr stop --session $SESSION"'""",
                ]
            )
        else:
            # Remote hosts: write a signal file and detach. The SSH wrapper script
            # reads the signal file after tmux exits and returns an exit code that
            # the local mngr process uses to run the appropriate command.
            signals_dir = self.host_dir / "signals"
            lines.extend(
                [
                    "# Ctrl-q: Write destroy signal and detach (handled by local mngr after SSH exits)",
                    f"""bind -n C-q run-shell 'SESSION=$(tmux display-message -p "#{{session_name}}"); mkdir -p {shlex.quote(str(signals_dir))}; echo destroy > {shlex.quote(str(signals_dir))}/"$SESSION"; tmux detach-client'""",
                    "",
                    "# Ctrl-t: Write stop signal and detach (handled by local mngr after SSH exits)",
                    f"""bind -n C-t run-shell 'SESSION=$(tmux display-message -p "#{{session_name}}"); mkdir -p {shlex.quote(str(signals_dir))}; echo stop > {shlex.quote(str(signals_dir))}/"$SESSION"; tmux detach-client'""",
                ]
            )

        config_content = "\n".join(lines)

        self.write_text_file(config_path, config_content)
        logger.debug("Created host tmux config at {}", config_path)

        return config_path

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        """Start agents by creating their tmux sessions.

        Creates a tmux session and uses send-keys to type and execute the command.
        This allows the user to hit ctrl-c and then up arrow to see and restart
        the command.

        If additional_commands are configured, creates new tmux windows in the
        same session for each additional command.

        Environment variables from the host and agent env files are sourced
        when creating the tmux session and its agent windows. The session's
        default-command is set to source env files and exec into the user's
        original default-command (queried via tmux show-option), so that
        user-created windows get both the env vars and the user's shell.

        A custom tmux config (holding only mngr's own settings; tmux loads the
        user's ~/.tmux.conf itself at server start) is used that:
        - Widens status-left-length and enables set-titles
        - Adds a Ctrl-q binding to detach and destroy the current agent
        - Adds a Ctrl-t binding to detach and halt (stop) the current agent

        All tmux commands, activity recording, and process monitor launch for
        each agent are batched into a single shell command to minimize network
        and process round trips (important for remote hosts).
        """
        with log_span("Starting {} agent(s)", len(agent_ids)):
            # Create the host-level tmux config (shared by all agents on this host)
            # This avoids the issue where per-agent configs would overwrite each other's
            # Ctrl-q bindings since tmux key bindings are server-wide
            tmux_config_path = self._create_host_tmux_config()

            onboarding_marker = self.mngr_ctx.profile_dir / "tmux_onboarding_shown"
            is_onboarding_needed = not onboarding_marker.exists() and os.environ.get("IS_AUTONOMOUS", "0") != "1"

            for agent_id in agent_ids:
                agent = self._get_agent_by_id(agent_id)
                if agent is None:
                    raise AgentNotFoundOnHostError(agent_id, self.id)

                # Before launching, reap any stale process tree from a prior incarnation
                # of this agent id -- but only when it isn't already running, so an
                # idempotent start never tears down a live agent. This clears orphans an
                # earlier abrupt teardown left behind (e.g. a bootstrap supervisord and
                # its ttyd reparented to PID 1) so the relaunch can't collide with the
                # survivors (e.g. EADDRINUSE on a fixed service port).
                if not self._does_agent_session_exist(agent):
                    for reap_failure in self.reap_agent_process_tree(agent):
                        logger.warning(
                            "Reaping a stale process for agent {} before start surfaced a cleanup issue: {}",
                            agent.name,
                            reap_failure,
                        )

                self._ensure_work_dir_exists(agent)

                command = self._get_agent_command(agent)
                additional_commands = self._get_agent_additional_commands(agent)

                onboarding_text: str | None = None
                if is_onboarding_needed:
                    is_onboarding_needed = False
                    onboarding_marker.touch()
                    if os.environ.get("TMUX"):
                        onboarding_text = ONBOARDING_TEXT_TMUX_USER
                    else:
                        onboarding_text = ONBOARDING_TEXT

                session_name = agent.session_name
                with log_span("Starting agent {} in tmux session {}", agent.name, session_name):
                    # Build and execute a single combined shell command for this agent
                    combined_command = _build_start_agent_shell_command(
                        agent=agent,
                        session_name=session_name,
                        command=command,
                        additional_commands=additional_commands,
                        env_shell_cmd=self._build_env_shell_command(agent),
                        tmux_config_path=tmux_config_path,
                        unset_vars=self.mngr_ctx.config.unset_vars,
                        host_dir=self.host_dir,
                        primary_window_name=self.mngr_ctx.config.tmux.primary_window_name,
                        tmux_options=self.get_agent_tmux_options(agent),
                        onboarding_text=onboarding_text,
                    )
                    result = self.execute_stateful_command(combined_command, cwd=agent.work_dir)
                    if not result.success:
                        raise AgentStartError(str(agent.name), result.stderr)

    def _run_classified_cleanup_command(
        self,
        command: str,
        *,
        failure_category: CleanupFailureCategory,
        benign_stderr_substrings: tuple[str, ...],
        agent_name: AgentName | None,
        timeout_seconds: float = _STOP_AGENT_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[CommandResult, CleanupFailure | None]:
        """Run one bounded shell step of the stop/cleanup path and classify its outcome.

        Returns the ``CommandResult`` (use ``.success`` / ``.stdout`` for the step's data)
        plus a ``CleanupFailure`` iff the step represents a *real* failure -- something
        actually left behind. ``None`` means success or a benign "already gone" outcome.

        Classification (see specs/cleanup-error-aggregation.md):
        - a timeout (the original hang bug) -> a ``TIMEOUT`` failure;
        - otherwise, a real failure iff stderr contains a non-empty line that matches none
          of ``benign_stderr_substrings``. A resource that was already gone surfaces as a
          recognized benign message (tmux "can't find session", kill ESRCH "No such
          process"), so it is filtered out.

        The exit code is intentionally not used for classification: several stop commands
        (the kill loop, ``pgrep`` with no matches) exit non-zero benignly, and message
        matching is robust to the session vanishing mid-teardown.
        """
        try:
            result = self.execute_idempotent_command(command, timeout_seconds=timeout_seconds, raise_on_timeout=True)
        except CommandTimeoutError as e:
            timeout_failure = CleanupFailure(
                category=CleanupFailureCategory.TIMEOUT,
                message=str(e),
                agent_name=agent_name,
                host_id=self.id,
            )
            logger.warning("Cleanup step timed out on host {}: {}", self.id, e)
            return CommandResult(stdout="", stderr=str(e), success=False), timeout_failure
        failure = self._classify_cleanup_command_stderr(
            command=command,
            stderr=result.stderr,
            failure_category=failure_category,
            benign_stderr_substrings=benign_stderr_substrings,
            agent_name=agent_name,
        )
        if failure is not None:
            logger.warning("Cleanup step left a resource behind on host {}: {}", self.id, failure.message)
        return result, failure

    def _classify_cleanup_command_stderr(
        self,
        *,
        command: str,
        stderr: str,
        failure_category: CleanupFailureCategory,
        benign_stderr_substrings: tuple[str, ...],
        agent_name: AgentName | None,
    ) -> CleanupFailure | None:
        """Return a CleanupFailure iff stderr has a line not explained by a benign cause."""
        offending = [
            line.strip()
            for line in stderr.splitlines()
            if line.strip() and not any(substring in line.lower() for substring in benign_stderr_substrings)
        ]
        if not offending:
            return None
        return CleanupFailure(
            category=failure_category,
            message=f"{command}: {'; '.join(offending)}",
            agent_name=agent_name,
            host_id=self.id,
        )

    def _get_all_descendant_pids(
        self,
        parent_pid: str,
        failures: list[CleanupFailure],
        agent_name: AgentName | None,
        visited: set[str] | None = None,
    ) -> list[str]:
        """Recursively get all descendant PIDs of a given parent PID.

        Appends any real failures (a ``pgrep`` error, or a timeout) to ``failures``;
        ``pgrep`` exiting 1 with no children is benign and produces no failure.

        Tracks already-visited PIDs in ``visited`` to break cycles that can
        appear via pid reuse (a long-lived process at pid X dies, the kernel
        recycles X as a descendant of one of its own descendants, and a
        naive walk loops forever). Without this, a sufficiently long-lived
        agent's destroy path could hit Python's recursion limit and crash
        the caller mid-cleanup.
        """
        if visited is None:
            visited = set()
        if parent_pid in visited:
            return []
        visited.add(parent_pid)
        descendant_pids: list[str] = []

        # Get immediate children. pgrep exits 1 (no stderr) when there are no children --
        # benign; a real pgrep error writes to stderr and is classified as a failure.
        result, failure = self._run_classified_cleanup_command(
            f"pgrep -P {parent_pid}",
            failure_category=CleanupFailureCategory.PROCESSES_REMAIN,
            benign_stderr_substrings=(),
            agent_name=agent_name,
        )
        if failure is not None:
            failures.append(failure)
        if result.success and result.stdout.strip():
            child_pids = result.stdout.strip().split("\n")
            for child_pid in child_pids:
                if child_pid and child_pid not in visited:
                    descendant_pids.append(child_pid)
                    # Recursively get descendants of this child
                    descendant_pids.extend(self._get_all_descendant_pids(child_pid, failures, agent_name, visited))

        return descendant_pids

    def _collect_session_pids(
        self, session_name: str, failures: list[CleanupFailure], agent_name: AgentName | None
    ) -> list[str]:
        """Collect all pane PIDs and their descendants for a tmux session.

        Appends any real failures (a tmux error, or a timeout) to ``failures``; a session
        or window that is already gone (tmux "can't find session/window") is benign and
        produces no failure.

        Iterates the session's windows and calls ``list-panes`` per window so
        every operation uses an exact-match target (``=<session>:<window>``).
        We avoid ``list-panes -s`` for the whole-session case: despite the man
        page calling its ``-t`` a target-session, tmux's ``cmd-find.c`` ignores
        the ``=`` exact-match prefix on ``-s``, so a bare-name target would
        silently fall back to session prefix matching -- letting a colliding
        sibling session's PIDs leak into the result and get killed downstream.
        The per-window iteration costs one extra tmux roundtrip per window
        (cheap, and this is a cleanup path) and removes the prefix-matching
        risk entirely.
        """
        windows_result, failure = self._run_classified_cleanup_command(
            f"tmux list-windows -t {TmuxSessionTarget(session_name=session_name).as_shell_arg()} -F '#I'",
            failure_category=CleanupFailureCategory.PROCESSES_REMAIN,
            benign_stderr_substrings=_TMUX_BENIGN_STDERR_SUBSTRINGS,
            agent_name=agent_name,
        )
        if failure is not None:
            failures.append(failure)
        if not windows_result.success or not windows_result.stdout.strip():
            return []

        all_pids: list[str] = []
        window_indices = [w.strip() for w in windows_result.stdout.strip().split("\n") if w.strip()]
        for window_idx in window_indices:
            window_target = TmuxWindowTarget(session_name=session_name, window=window_idx)
            result, failure = self._run_classified_cleanup_command(
                f"tmux list-panes -t {window_target.as_shell_arg()} -F '#{{pane_pid}}'",
                failure_category=CleanupFailureCategory.PROCESSES_REMAIN,
                benign_stderr_substrings=_TMUX_BENIGN_STDERR_SUBSTRINGS,
                agent_name=agent_name,
            )
            if failure is not None:
                failures.append(failure)
            if result.success and result.stdout.strip():
                pane_pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
                for pane_pid in pane_pids:
                    all_pids.append(pane_pid)
                    all_pids.extend(self._get_all_descendant_pids(pane_pid, failures, agent_name))
        return all_pids

    def _collect_pids_by_agent_id_env(
        self, agent_id: AgentId, failures: list[CleanupFailure], agent_name: AgentName | None
    ) -> list[str]:
        """Find all PIDs whose MNGR_AGENT_ID environment matches agent_id.

        Appends a failure to ``failures`` only if the scan itself times out; the command
        is constructed to exit 0 (see the ``; true`` below) and suppresses per-pid grep
        noise, so it does not otherwise produce a real failure.

        The agent's env file (sourced via `set -a`) exports MNGR_AGENT_ID into every
        process spawned in its tmux session. Scanning by env var catches orphans
        whose ancestor died abruptly (SIGKILL, OOM, segfault) -- their reparenting
        to PID 1 hides them from the tmux pane / pgrep -P descendant walk.

        Linux: walks /proc/<pid>/environ. The file is NUL-separated KEY=VALUE
        records, so `grep -z` is used and `^` anchors at the start of each record
        (see the inline comment below for why anchoring matters). macOS: ps -E
        does not expose env for processes once they've reparented to launchd
        (SIP restriction), so this is a best-effort no-op there -- the tree walk
        handles the typical macOS case where the pane process is still alive.

        Why an env-marker scan instead of a process-group / setsid mechanism: prior
        attempts to manage the agent process tree via process groups have been
        deliberately retired. setsid-wrapping the pane command was removed in
        c4ac00242c (forked-and-exited intermediate caused a start_agents race),
        and `kill -- -<pgid>` was abandoned in 4ebf66d2f4 because bash job control
        in interactive panes puts every backgrounded process (e.g. `npm exec ... &`
        spawned by claude) into its own pgrp, so the pane's pgrp does not cover the
        descendants we need to kill. Env-marker inheritance survives both job
        control and reparenting without re-introducing the issues those commits
        fixed.
        """
        # AgentId is `agent-<32 hex chars>` (see RandomId), so the value is
        # regex-safe and does not need escaping for grep BRE.
        quoted_id = shlex.quote(str(agent_id))
        # SELF excludes our own scan shell so a caller running inside an agent that
        # happens to inherit the env doesn't kill itself.
        #
        # The grep pattern is anchored with `^` so it matches only env vars
        # *named* MNGR_AGENT_ID. Under `grep -z`, `^` matches the start of each
        # NUL-separated record (i.e. the start of each KEY=VALUE pair), so a
        # hypothetical env var like `OTHER_MNGR_AGENT_ID=...` cannot trigger a
        # false match. We use BRE (drop -F) because -F has no anchors.
        #
        # Trailing `; true` forces a clean exit: the for loop's exit code is the
        # last iteration's `[ -r ... ] && grep ... && echo ...` chain, which is 1
        # when the final PID doesn't match (the common case). Without `; true`,
        # `result.success` would be False even when stdout contains real matches,
        # and the env-scan fallback would silently no-op. We rely on stdout
        # content alone -- the exit code carries no useful signal here.
        cmd = (
            f"AGENT_ID={quoted_id}; "
            "SELF=$$; "
            'if [ "$(uname -s)" = "Linux" ]; then '
            "  for d in /proc/[0-9]*; do "
            "    pid=${d##*/}; "
            '    [ "$pid" = "$SELF" ] && continue; '
            '    [ -r "$d/environ" ] && grep -qza "^MNGR_AGENT_ID=$AGENT_ID" "$d/environ" 2>/dev/null && echo "$pid"; '
            "  done; "
            "fi; true"
        )
        result, failure = self._run_classified_cleanup_command(
            cmd,
            failure_category=CleanupFailureCategory.PROCESSES_REMAIN,
            benign_stderr_substrings=(),
            agent_name=agent_name,
        )
        if failure is not None:
            failures.append(failure)
        if not result.stdout.strip():
            return []
        return [pid for pid in result.stdout.strip().split("\n") if pid.strip()]

    def _collect_agent_process_pids(self, agent: AgentInterface, failures: list[CleanupFailure]) -> list[str]:
        """Collect every pid belonging to ``agent``: the pane descendants of its tmux
        session plus any ``MNGR_AGENT_ID``-tagged orphans that reparented to PID 1 after
        an abrupt ancestor death. Deduplicated, order-preserving.
        """
        session_name = self.mngr_ctx.config.agent_session_name(agent.name)
        pids = self._collect_session_pids(session_name, failures, agent.name)
        pids.extend(self._collect_pids_by_agent_id_env(agent.id, failures, agent.name))
        return list(dict.fromkeys(pids))

    def _terminate_pids(self, pids: Sequence[str], failures: list[CleanupFailure], *, timeout_seconds: float) -> None:
        """SIGTERM then (after a short grace) SIGKILL every pid, in one batched shell
        command so a single unresponsive process can't starve the others of SIGKILL.
        """
        if not pids:
            return
        pid_list = " ".join(pids)
        # Send SIGTERM to all processes at once, then wait briefly and SIGKILL survivors.
        # Batching avoids the issue where one non-responsive process (e.g. interactive
        # bash that ignores SIGTERM) consumes the whole timeout budget in a serial loop.
        #
        # stderr is captured (no `2>/dev/null`) so kill failures can be classified: a pid
        # that died between collection and the kill (ESRCH "No such process") is benign,
        # while an unkillable process (EPERM) is a real PROCESSES_REMAIN failure.
        grace_seconds = min(1.0, timeout_seconds)
        _result, failure = self._run_classified_cleanup_command(
            f"for p in {pid_list}; do kill -TERM $p; done; "
            f"sleep {grace_seconds}; "
            f"for p in {pid_list}; do kill -KILL $p; done",
            failure_category=CleanupFailureCategory.PROCESSES_REMAIN,
            benign_stderr_substrings=_KILL_BENIGN_STDERR_SUBSTRINGS,
            agent_name=None,
            # Bound by the grace sleep plus a fixed margin so a stuck kill loop can't hang
            # cleanup; the command itself only sleeps once.
            timeout_seconds=grace_seconds + _STOP_AGENT_COMMAND_TIMEOUT_SECONDS,
        )
        if failure is not None:
            failures.append(failure)

    def reap_agent_process_tree(self, agent: AgentInterface, *, timeout_seconds: float = 5.0) -> list[CleanupFailure]:
        """Kill every process belonging to ``agent`` (pane/session descendants plus any
        ``MNGR_AGENT_ID``-tagged orphans reparented to PID 1) with SIGTERM then SIGKILL.

        Does NOT kill the tmux session itself -- callers that want a full stop do that
        separately. Returns the cleanup failures so the caller decides whether to raise
        (a stop) or log and proceed (a (re)start).

        This is the single mechanism that guarantees a long-lived daemon launched under
        an agent (e.g. the FCT bootstrap's ``supervisord`` and its children, like a ttyd
        bound to a fixed port) cannot outlive the agent: the env marker is inherited by
        every descendant and survives reparenting, so it catches orphans that a
        pane/process-tree walk would miss.
        """
        failures: list[CleanupFailure] = []
        pids = self._collect_agent_process_pids(agent, failures)
        self._terminate_pids(pids, failures, timeout_seconds=timeout_seconds)
        return failures

    def _does_agent_session_exist(self, agent: AgentInterface) -> bool:
        """Return True iff the agent's tmux session already exists (it is likely running)."""
        session_name = self.mngr_ctx.config.agent_session_name(agent.name)
        target = TmuxSessionTarget(session_name=session_name).as_shell_arg()
        result = self.execute_idempotent_command(
            f"tmux has-session -t {target} 2>/dev/null",
            timeout_seconds=_STOP_AGENT_COMMAND_TIMEOUT_SECONDS,
            raise_on_timeout=False,
        )
        return result.success

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        """Stop agents by killing all processes in their tmux sessions.

        Returns normally if every agent was fully stopped or only benign "already gone"
        outcomes occurred; raises ``CleanupFailedGroup`` carrying the *real* cleanup
        failures (resources left behind) otherwise. Every step is best-effort and bounded:
        a failure on one step or agent is recorded and the rest still run (see
        specs/cleanup-error-aggregation.md).

        This ensures all processes in all panes are terminated by:
        1. Getting all PIDs (panes + descendants + orphans matched by MNGR_AGENT_ID env)
        2. Sending SIGTERM to each individual process
        3. Waiting briefly, then sending SIGKILL to any survivors
        4. Finally killing the tmux session itself
        """
        with log_span("Stopping {} agent(s) with timeout={}s", len(agent_ids), timeout_seconds):
            with collecting_cleanup_failures() as failures:
                all_pids: list[str] = []

                current_agents: list[AgentInterface] = []

                for agent_id in agent_ids:
                    agent = self._get_agent_by_id(agent_id)
                    if agent is None:
                        continue

                    current_agents.append(agent)
                    # Pane descendants plus orphans (e.g. children of an OOM-killed claude,
                    # or a bootstrap daemon reparented to PID 1) matched by MNGR_AGENT_ID.
                    all_pids.extend(self._collect_agent_process_pids(agent, failures))

                # Deduplicate while preserving order (a pid may appear under >1 agent).
                # The kill is batched across all agents, so a failure is not attributed
                # to a single agent.
                all_pids = list(dict.fromkeys(all_pids))
                self._terminate_pids(all_pids, failures, timeout_seconds=timeout_seconds)

                # Finally kill the tmux sessions themselves. A session already gone (tmux
                # "can't find session") is benign; a session that exists but cannot be killed
                # is a real LOCAL_STATE_REMAINS failure.
                for agent in current_agents:
                    session_name = self.mngr_ctx.config.agent_session_name(agent.name)
                    _result, failure = self._run_classified_cleanup_command(
                        f"tmux kill-session -t {TmuxSessionTarget(session_name=session_name).as_shell_arg()}",
                        failure_category=CleanupFailureCategory.LOCAL_STATE_REMAINS,
                        benign_stderr_substrings=_TMUX_BENIGN_STDERR_SUBSTRINGS,
                        agent_name=agent.name,
                    )
                    if failure is not None:
                        failures.append(failure)

    def _get_agent_by_id(self, agent_id: AgentId) -> AgentInterface | None:
        """Get an agent by ID."""
        agents = self.get_agents()
        for agent in agents:
            if agent.id == agent_id:
                return agent
        return None

    def _get_agent_command(self, agent: AgentInterface) -> str:
        """Get the command for an agent."""
        data_path = get_agent_state_dir_path(self.host_dir, agent.id) / "data.json"
        try:
            content = self.read_text_file(data_path)
        except FileNotFoundError as e:
            raise NoCommandDefinedError(f"No data.json file for agent {agent.name} ({agent.id})") from e

        data = json.loads(content)
        try:
            return data["command"]
        except KeyError as e:
            raise NoCommandDefinedError(f"No command in data.json for agent {agent.name} ({agent.id})") from e

    def _get_agent_additional_commands(self, agent: AgentInterface) -> list[NamedCommand]:
        """Get the additional commands for an agent."""
        data_path = get_agent_state_dir_path(self.host_dir, agent.id) / "data.json"
        try:
            content = self.read_text_file(data_path)
        except FileNotFoundError:
            return []

        data = json.loads(content)
        raw_commands = data.get("additional_commands", [])

        # Handle both old format (list of strings) and new format (list of dicts)
        result: list[NamedCommand] = []
        for cmd in raw_commands:
            if isinstance(cmd, str):
                # Old format: plain string
                result.append(NamedCommand(command=cmd, window_name=None))
            else:
                # New format: dict with command and window_name
                result.append(NamedCommand(command=cmd["command"], window_name=cmd.get("window_name")))
        return result

    def get_agent_tmux_options(self, agent: AgentInterface) -> AgentTmuxOptions:
        """Read the agent's persisted tmux window options from data.json.

        Returns default (all-None) options when there is no data.json or no tmux
        block, so older agents created before this field existed behave as before.
        """
        data_path = get_agent_state_dir_path(self.host_dir, agent.id) / "data.json"
        try:
            content = self.read_text_file(data_path)
        except FileNotFoundError:
            return AgentTmuxOptions()
        data = json.loads(content)
        return AgentTmuxOptions.from_data_dict(data.get("tmux"))

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================

    def get_idle_seconds(self) -> float:
        """Get the number of seconds since last activity.

        Checks both host-level activity files (like BOOT) and agent-level
        activity files (like CREATE, START, AGENT). Returns the time since
        the most recent activity from any source.
        """
        latest_activity: datetime | None = None

        # Check host-level activity files
        for activity_type in ActivitySource:
            activity_time = self.get_reported_activity_time(activity_type)
            if activity_time is not None:
                if latest_activity is None or activity_time > latest_activity:
                    latest_activity = activity_time

        # Check agent-level activity files for all agents on this host
        for agent in self.get_agents():
            for activity_type in ActivitySource:
                activity_time = agent.get_reported_activity_time(activity_type)
                if activity_time is not None:
                    if latest_activity is None or activity_time > latest_activity:
                        latest_activity = activity_time

        if latest_activity is None:
            return float("inf")

        now = datetime.now(timezone.utc)
        return (now - latest_activity).total_seconds()

    def get_state(self) -> HostState:
        """Get the current state of the host."""
        if self.is_local:
            logger.trace("Determined host {} is local, state=RUNNING", self.id)
            return HostState.RUNNING

        try:
            result = self.execute_idempotent_command("echo ok")
            if result.success:
                logger.trace("Determined host {} state=RUNNING (ping successful)", self.id)
                return HostState.RUNNING
        except (OSError, HostConnectionError):
            pass

        # otherwise use the offline logic
        return super().get_state()


ONBOARDING_TEXT = """\
Welcome to your first agent!

Mngr runs your agents in tmux sessions.
If you have never used tmux, here is the official tutorial:
https://github.com/tmux/tmux/wiki/Getting-Started

Here are some useful keybindings:

  Ctrl-b d     Detach (return to shell)
  Ctrl-b [     Scroll / copy mode
  Ctrl-q       Destroy agent
  Ctrl-t       Stop agent

To reconnect later, run:

  mngr connect

This popup won't show again in future sessions."""

ONBOARDING_TEXT_TMUX_USER = """\
Welcome to your first agent!

Mngr runs your agents in tmux sessions,
and I can see you're already a tmux user.
Here are some tips for using mngr alongside tmux:
https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/tmux_users.md

The config file for mngr's tmux sessions is ~/.mngr/tmux.conf.
Among other things, it sets up some extra keybindings:

  Ctrl-q       Destroy agent
  Ctrl-t       Stop agent

To reconnect later, run:

  mngr connect

This popup won't show again in future sessions."""


@pure
def _parse_porcelain_line(line: str) -> list[str]:
    """Parse a git status --porcelain line and return filenames to transfer.

    The porcelain format is ``XY filename`` where X is the index status and Y
    is the work-tree status. Files with status D (deleted) in either position
    cannot be rsynced because they no longer exist on disk, so they are skipped.
    Renames (``old -> new``) return only the new filename.
    """
    if len(line) < 4:
        return []
    status_x = line[0]
    status_y = line[1]
    # Skip deleted files -- they don't exist on disk and can't be transferred
    if status_x == "D" or status_y == "D":
        return []
    filename = line[3:]
    if " -> " in filename:
        filename = filename.split(" -> ")[1]
    return [filename]


@pure
def _build_start_agent_shell_command(
    agent: AgentInterface,
    session_name: str,
    command: str,
    additional_commands: Sequence[NamedCommand],
    env_shell_cmd: str,
    tmux_config_path: Path,
    unset_vars: Sequence[str],
    host_dir: Path,
    primary_window_name: str,
    tmux_options: AgentTmuxOptions,
    onboarding_text: str | None = None,
) -> str:
    """Build a single shell command that starts an agent and its tmux session.

    Combines all tmux operations, activity recording, and process monitor
    launch into one command to minimize network round trips for remote hosts.

    The command chains critical steps with && so that if any step fails,
    subsequent steps are skipped. The process activity monitor is launched
    in a subshell so it runs in the background without affecting the chain.

    If the tmux session already exists, the command exits early (successfully)
    since everything has presumably already been set up.
    """
    # Bail out early if the session already exists. stderr is redirected to
    # suppress the "can't find session" message when the session doesn't exist yet.
    quoted_exact_session = TmuxSessionTarget(session_name=session_name).as_shell_arg()
    guard = f"tmux has-session -t {quoted_exact_session} 2>/dev/null && exit 0"

    steps: list[str] = []

    # Unset environment variables
    for var_name in unset_vars:
        steps.append(f"unset {shlex.quote(var_name)}")

    # Create a detached tmux session with env vars sourced.
    # Explicitly set -x/-y to force tmux to initialize the PTY dimensions
    # directly. Without these flags, the pane's logical size (per list-panes)
    # is 80x24 from default-size, but the PTY's TIOCGWINSZ can report 0x0 or
    # 1x1 to the process inside it when the server has a narrow attached
    # client (e.g. user running from a split terminal). This causes Claude
    # Code's Ink framework to render at 1 column wide, breaking marker-based
    # message sending. Passing -x/-y appears to use a different tmux code
    # path that sets the PTY dimensions correctly at creation time.
    # Name the primary window (-n) and target it by name everywhere below, so
    # mngr's targeting is independent of the user's tmux `base-index` setting
    # (a `set -g base-index 1` in ~/.tmux.conf would otherwise create the agent
    # window at index 1 while mngr hardcoded `:0`).
    # Width/height come from the agent's tmux options (falling back to the
    # historical 200x50). Unless window-size is "manual", the window will still be
    # resized to match the client's terminal when attached.
    tmux_width = int(tmux_options.width) if tmux_options.width is not None else _DEFAULT_TMUX_WIDTH
    tmux_height = int(tmux_options.height) if tmux_options.height is not None else _DEFAULT_TMUX_HEIGHT
    steps.append(
        "tmux new-session -d"
        f" -s {shlex.quote(session_name)}"
        f" -n {shlex.quote(primary_window_name)}"
        f" -x {tmux_width} -y {tmux_height}"
        f" -c {shlex.quote(str(agent.work_dir))}"
        f" {shlex.quote(env_shell_cmd)}"
    )

    # Source mngr's own tmux config (options and key bindings) at agent creation;
    # the user's own config is pulled in at tmux server start. Parenthesized so the
    # '|| true' (a cosmetic-config error must not block startup) scopes to this step,
    # not the whole && chain.
    steps.append(f"(tmux source-file {shlex.quote(str(tmux_config_path))} || true)")

    quoted_exact_agent_window = TmuxWindowTarget(session_name=session_name, window=primary_window_name).as_shell_arg()

    # Apply the requested resize policy (e.g. "manual" pins the window to the
    # dimensions above so attaching clients never resize it). window-size is a
    # window option, so it is set on the agent's primary window. When unset, tmux's
    # own default ("latest") is left in place -- today's behavior.
    if tmux_options.window_size is not None:
        steps.append(
            f"tmux set-option -t {quoted_exact_agent_window} window-size {tmux_options.window_size.value.lower()}"
        )

    # Save the user's original default-command (from their ~/.tmux.conf) into
    # the tmux session environment, then set default-command to env_shell_cmd.
    # Because env_shell_cmd uses ${MNGR_SAVED_DEFAULT_TMUX_COMMAND:-${SHELL:-bash}},
    # the initial agent window (created above, before this variable exists) gets
    # the user's login shell, while user-created windows get the saved default.
    save_user_shell_script = (
        f"U=$(tmux show-option -t {quoted_exact_agent_window} -Aqv default-command 2>/dev/null); "
        f'[ -z "$U" ] && U=$(tmux show-option -t {quoted_exact_agent_window} -Aqv default-shell 2>/dev/null) || true; '
        '[ -z "$U" ] && U="${SHELL:-bash}"; '
        f'tmux set-environment -t {quoted_exact_session} MNGR_SAVED_DEFAULT_TMUX_COMMAND "$U"'
    )
    steps.append("bash -c " + shlex.quote(save_user_shell_script))
    steps.append(f"tmux set-option -t {quoted_exact_agent_window} default-command {shlex.quote(env_shell_cmd)}")

    # Set a one-shot client-attached hook that shows the onboarding popup
    # when the user first attaches to this tmux session. This must happen
    # before send-keys triggers the agent command, because fast-exiting
    # commands (e.g. echo && exit 0) can destroy the session before later
    # steps in the && chain execute.
    if onboarding_text is not None:
        # The popup appends a blank line and "Press Enter to continue..." after the text
        full_text = onboarding_text + "\n\nPress Enter to continue..."
        lines = full_text.split("\n")
        # +2 for the tmux popup border
        popup_w = max(len(line) for line in lines) + 4
        popup_h = len(lines) + 2
        printf_text = onboarding_text.replace('"', '\\"').replace("\n", "\\n")
        popup_shell_cmd = f'printf "{printf_text}\\n\\nPress Enter to continue...\\n" && read'
        # Escape double quotes for the tmux command context: display-popup -E "..."
        tmux_escaped = popup_shell_cmd.replace('"', '\\"')
        hook_value = (
            f'display-popup -w {popup_w} -h {popup_h} -E "{tmux_escaped}"'
            f" ; set-hook -u -t {quoted_exact_agent_window}"
            f" client-attached[99]"
        )
        steps.append(f"tmux set-hook -t {quoted_exact_agent_window} client-attached[99] {shlex.quote(hook_value)}")

    # Create additional windows BEFORE sending the agent command. This
    # ensures all windows exist before the agent starts, preventing a race
    # where a fast-exiting agent command destroys the session before
    # additional windows can be created.
    for idx, named_cmd in enumerate(additional_commands):
        window_name = named_cmd.window_name if named_cmd.window_name else f"cmd-{idx + 1}"
        quoted_exact_named_window = TmuxWindowTarget(session_name=session_name, window=window_name).as_shell_arg()

        steps.append(
            f"tmux new-window -t {quoted_exact_session}"
            f" -n {shlex.quote(window_name)}"
            f" -c {shlex.quote(str(agent.work_dir))}"
            f" {shlex.quote(env_shell_cmd)}"
        )
        steps.append(f"tmux send-keys -t {quoted_exact_named_window} -l -- {shlex.quote(str(named_cmd.command))}")
        steps.append(f"tmux send-keys -t {quoted_exact_named_window} Enter")

    # If we created additional windows, select the first window (the main agent)
    # before sending the agent command
    if additional_commands:
        steps.append(f"tmux select-window -t {quoted_exact_agent_window}")

    # Send the agent command as literal keys, then Enter to execute.
    # Target the agent's primary window by name explicitly so this works even
    # after additional windows have been created (which changes the active window).
    steps.append(f"tmux send-keys -t {quoted_exact_agent_window} -l -- {shlex.quote(command)}")
    steps.append(f"tmux send-keys -t {quoted_exact_agent_window} Enter")

    # Record START activity for idle detection by writing JSON to the activity file
    # The authoritative activity time is the file's mtime, not the JSON content
    activity_dir = get_agent_state_dir_path(host_dir, agent.id) / "activity"
    activity_path = activity_dir / ActivitySource.START.value.lower()
    steps.append(f"mkdir -p {shlex.quote(str(activity_dir))}")
    activity_printf_cmd = (
        'printf \'{"time": %s, "agent_id": "%s", "agent_name": "%s"}\\n\''
        f' "$(($(date +%s) * 1000))"'
        f" {shlex.quote(str(agent.id))}"
        f" {shlex.quote(str(agent.name))}"
        f" > {shlex.quote(str(activity_path))}"
    )
    steps.append(activity_printf_cmd)

    # Build the process activity monitor script (runs in the background, inspects the agent's primary window where the agent is assumed to be running)
    # Wait up to 10 seconds for the PANE_PID to appear (tmux can take a moment to start)
    max_wait_seconds = 10
    tmux_list_panes_cmd = f"tmux list-panes -t {quoted_exact_agent_window} -F '#{{pane_pid}}' 2>/dev/null | head -n 1"
    process_activity_path = activity_dir / ActivitySource.PROCESS.value.lower()
    monitor_script = (
        f"PANE_PID=$({tmux_list_panes_cmd}); "
        f"TRIES=0; "
        f'while [ -z "$PANE_PID" ] && [ "$TRIES" -lt {max_wait_seconds} ]; do '
        f"sleep 1; "
        f"TRIES=$((TRIES + 1)); "
        f"PANE_PID=$({tmux_list_panes_cmd}); "
        f"done; "
        'if [ -z "$PANE_PID" ]; then exit 0; fi; '
        f"ACTIVITY_PATH={shlex.quote(str(process_activity_path))}; "
        f"AGENT_ID={shlex.quote(str(agent.id))}; "
        'mkdir -p "$(dirname "$ACTIVITY_PATH")"; '
        'while kill -0 "$PANE_PID" 2>/dev/null; do '
        "TIME_MS=$(($(date +%s) * 1000)); "
        'printf \'{\\n  "time": %d,\\n  "pane_pid": %s,\\n  "agent_id": "%s"\\n}\\n\''
        ' "$TIME_MS" "$PANE_PID" "$AGENT_ID" > "$ACTIVITY_PATH"; '
        "sleep 5; "
        "done"
    )

    # Launch the monitor in a subshell so the & only backgrounds the nohup,
    # not the entire && chain
    monitor_cmd = f"(nohup bash -c {shlex.quote(monitor_script)} </dev/null >/dev/null 2>&1 &)"
    steps.append(monitor_cmd)

    return guard + "; " + " && ".join(steps)


@pure
def _parse_uptime_output(stdout: str) -> float:
    """Parse the output of the cross-platform uptime command.

    Handles two formats:
    - macOS: two lines (boot timestamp, current timestamp) from sysctl + date
    - Linux: single line from /proc/uptime (uptime_seconds idle_seconds)
    """
    output = stdout.strip()
    output_lines = output.split("\n")
    try:
        if len(output_lines) == 2:
            # macOS: two lines -- boot time and current time
            boot_time = int(output_lines[0])
            current_time = int(output_lines[1])
            return float(current_time - boot_time)
        elif len(output_lines) == 1 and output:
            # Linux: single line from /proc/uptime
            uptime_str = output.split()[0]
            return float(uptime_str)
        else:
            return 0.0
    except (ValueError, OSError):
        return 0.0


@pure
def _parse_boot_time_output(stdout: str) -> datetime | None:
    """Parse the output of the cross-platform boot time command.

    Both macOS (sysctl) and Linux (btime) produce a single Unix timestamp.
    """
    try:
        boot_timestamp = int(stdout.strip())
        return datetime.fromtimestamp(boot_timestamp, tz=timezone.utc)
    except (ValueError, OSError):
        return None


@pure
def _format_env_file(env: Mapping[str, str]) -> str:
    """Format a dict as an environment file."""
    lines: list[str] = []
    for key, value in env.items():
        if " " in value or '"' in value or "'" in value or "\n" in value:
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"
