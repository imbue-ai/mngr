"""``mngr_opencode`` plugin -- registers the ``opencode`` agent type for OpenCode.

OpenCode (https://opencode.ai) is an open-source terminal AI coding agent and,
unlike Claude Code / Antigravity, a **client-server** app: a server owns the
sessions and an event bus, and TUI / CLI / HTTP clients talk to it. mngr leans
into that shape rather than screen-scraping a TUI.

How an opencode agent runs (see ``resources/opencode_launch.sh``)
-----------------------------------------------------------------
The agent's tmux pane runs ``opencode_launch.sh``, which starts two processes:

* a headless ``opencode serve`` (the SERVER) on a per-agent port -- the
  in-process lifecycle plugin's event hook runs here, maintaining the ``active``
  marker (RUNNING vs WAITING) and the raw transcript; and
* an ``opencode attach`` TUI CLIENT in the foreground -- what the user sees via
  ``mngr connect``, and what process-name lifecycle detection keys off (both
  processes report ``opencode``).

The script pre-creates the session (or reuses the recorded one on restart) so
the client attaches to a known session, records its id and the server's bound
port, then attaches.

Sending messages
----------------
``send_message`` POSTs the message to the agent's server (``prompt_async`` on the
host via ``curl``), and the attached client renders it -- so the conversation is
fully visible in ``mngr connect`` without typing into the TUI. This avoids the
keystroke-paste race entirely (OpenCode drops keys during its post-launch
repaint) and is structured rather than screen-scraped.

Per-agent isolation
-------------------
``OPENCODE_CONFIG_DIR`` (config + the auto-loaded lifecycle plugin) and
``XDG_DATA_HOME`` (db, ``auth.json``, storage, logs -- so sessions and
credentials are per-agent), injected only on the OpenCode processes. The
preferred config-dir shape; no ``$HOME`` relocation. Auth is shared by
symlinking the per-agent ``auth.json`` to ``~/.local/share/opencode/auth.json``
(OpenCode writes it in place, so one login authenticates all agents).

Transcript: the plugin writes the raw transcript in-process; a backgrounded
converter (``resources/opencode_common_transcript.sh``) turns it into the common
format ``mngr transcript`` reads. Trust/onboarding: OpenCode has no blocking
first-run dialog (verified live), so nothing needs seeding.
"""

from __future__ import annotations

import importlib.resources
import shlex
import shutil
import tempfile
import urllib.parse
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import verify_pinned_cli_version
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import is_self_update_disabled
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import copy_on_host
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasPermissionPolicyMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_opencode import resources as _opencode_resources
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import AGENT_OPENCODE_DB_RELPATH
from imbue.mngr_opencode.opencode_config import AGENT_OPENCODE_STORE_RELPATH
from imbue.mngr_opencode.opencode_config import EMIT_COMMON_ENABLED_VALUE
from imbue.mngr_opencode.opencode_config import EMIT_COMMON_ENV_VAR
from imbue.mngr_opencode.opencode_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_opencode.opencode_config import NATIVE_DB_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import NATIVE_DB_SHM_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import NATIVE_DB_WAL_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import NATIVE_STORAGE_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import OPENCODE_BIN_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_PORT_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_WORKDIR_ENV_VAR
from imbue.mngr_opencode.opencode_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_opencode.opencode_config import PLUGIN_FILENAME
from imbue.mngr_opencode.opencode_config import READY_SENTINEL_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import apply_opencode_merge
from imbue.mngr_opencode.opencode_config import apply_opencode_rebind
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import collect_adopt_search_db_paths
from imbue.mngr_opencode.opencode_config import get_opencode_app_data_dir
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_opencode_root_session_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_server_port_file_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.opencode_config import read_only_root_session_id
from imbue.mngr_opencode.opencode_config import read_opencode_config
from imbue.mngr_opencode.opencode_config import resolve_adopt_session_db
from imbue.mngr_opencode.opencode_config import serialize_opencode_config

# User's global OpenCode config, the base for the per-agent opencode.json when
# ``sync_global_config`` is set. Lives under the default XDG config dir; honoring
# a custom ``$XDG_CONFIG_HOME`` is a possible future refinement.
_USER_CONFIG_RELATIVE_PATH: Final[tuple[str, ...]] = (".config", "opencode", "opencode.json")

# OpenCode env vars that isolate config and data per agent.
_OPENCODE_CONFIG_DIR_ENV_VAR: Final[str] = "OPENCODE_CONFIG_DIR"
_XDG_DATA_HOME_ENV_VAR: Final[str] = "XDG_DATA_HOME"

# Ask ``opencode serve`` for an OS-assigned free port (verified: concurrent
# ``--port 0`` servers get distinct ports). The launch script records the actual
# bound port, so co-resident agents never collide and there is no port to pick.
_EPHEMERAL_PORT: Final[str] = "0"

# How long to wait for the launch script's readiness sentinel (server up +
# session created). Generous because a cold start migrates OpenCode's SQLite db.
_READY_TIMEOUT_SECONDS: Final[float] = 30.0
_READY_POLL_INTERVAL_SECONDS: Final[float] = 0.25

# OpenCode server endpoint that enqueues a prompt without blocking on the reply
# (the agent's lifecycle marker tracks completion, so send is fire-and-forget).
_PROMPT_ENDPOINT_TEMPLATE: Final[str] = "http://127.0.0.1:{port}/session/{session_id}/prompt_async"

# OpenCode's native db file name plus its WAL sidecars. Adoption builds the agent db on a LOCAL
# staging path and copies exactly this trio onto the (possibly remote) host -- the ``-wal``/``-shm``
# sidecars carry writes not yet checkpointed into the main file, so they travel with it.
_DB_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = ("-wal", "-shm")


def _build_prompt_post_command(port: str, session_id: str, message: str) -> str:
    """Build the host ``curl`` command that POSTs ``message`` to the agent's server.

    The message is sent as a JSON text part (so newlines/quotes are carried
    safely) and both the URL and JSON body are shell-quoted.
    """
    url = _PROMPT_ENDPOINT_TEMPLATE.format(port=port, session_id=session_id)
    payload = serialize_opencode_config({"parts": [{"type": "text", "text": message}]})
    return f"curl -fsS -X POST {shlex.quote(url)} -H 'content-type: application/json' -d {shlex.quote(payload)}"


def _load_opencode_resource(filename: str) -> str:
    """Load a resource file from the mngr_opencode resources package."""
    resource_files = importlib.resources.files(_opencode_resources)
    return resource_files.joinpath(filename).read_text()


def _adopt_search_db_paths(mngr_ctx: MngrContext) -> list[Path]:
    """The ``opencode.db`` paths an ``--adopt`` session id is searched across (local only).

    The user-native db plus every live and preserved local mngr agent's db. Used by both the
    ``on_before_create`` fail-fast and the ``adopt_session`` resolution so they search the
    same set.
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    agent_db_paths = iter_agent_session_paths(local_host_dir, AGENT_OPENCODE_DB_RELPATH)
    return collect_adopt_search_db_paths(agent_db_paths)


class OpenCodeAgentConfig(AgentTypeConfig):
    """Config for the opencode agent type."""

    command: CommandString = Field(
        default=CommandString("opencode"),
        description="Command to run the opencode agent.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Extra arguments forwarded to the opencode attach (TUI) client.",
    )
    # config_overrides mirrors mngr_antigravity's settings_overrides: a free-form
    # blob merged last into the per-agent opencode.json. Covers ``model``
    # ("provider/model"), the ``permission`` policy block ({"bash": {"git *":
    # "allow", "rm -rf *": "deny"}, "edit": "ask", ...}), ``small_model``, etc.
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key/value blob merged last into the per-agent opencode.json "
        "(e.g. model, the permission policy block). "
        'Example: {"model": "anthropic/claude-sonnet-4-5", "permission": {"bash": {"rm -rf *": "deny"}}}.',
    )
    # sync_global_config mirrors mngr_antigravity's sync_home_settings: when True
    # (default), the per-agent opencode.json starts from a copy of the user's real
    # ~/.config/opencode/opencode.json; config_overrides layer on top. When False,
    # the base is an empty config.
    sync_global_config: bool = Field(
        default=True,
        description="Base the per-agent opencode.json on a copy of the user's "
        "~/.config/opencode/opencode.json, or start from an empty base.",
    )
    # symlink_auth mirrors mngr_antigravity's symlink_oauth_token. With the
    # default (symlink), the per-agent auth.json symlinks to the shared
    # ~/.local/share/opencode/auth.json so one agent's login authenticates all
    # agents (and refreshes propagate). Copy mode (False) gives full isolation.
    symlink_auth: bool = Field(
        default=True,
        description="Symlink the per-agent auth.json to the shared "
        "~/.local/share/opencode/auth.json, so one login authenticates all agents. "
        "Set False for full isolation.",
    )
    # auto_allow_permissions injects a wildcard ``permission`` allow into the
    # per-agent opencode.json (auto-approve every action not explicitly denied) --
    # the config analog of OpenCode's ``run --dangerously-skip-permissions``.
    auto_allow_permissions: bool = Field(
        default=False,
        description="Auto-approve everything not explicitly denied "
        "(injects a wildcard allow into the opencode.json permission block).",
    )
    check_installation: bool = Field(
        default=True,
        description="Check whether opencode is installed and install it if missing "
        "(if False, assume it is already present).",
    )
    version: str | None = Field(
        default=None,
        description="Pin the opencode version to install (e.g., '0.4.10'). When set, installation runs the "
        "opencode installer with VERSION=<version> and provisioning verifies the installed opencode matches, "
        "erroring on a mismatch. When None (the default), installs the latest version.",
    )
    update_policy: AgentUpdatePolicy | None = Field(
        default=None,
        description='How to handle opencode\'s startup auto-update. NEVER sets `"autoupdate": false` in the '
        "per-agent opencode.json so opencode does not update itself on launch; AUTO leaves auto-update enabled. "
        "ASK has no interactive flow for opencode and behaves like AUTO. When unset (the default), resolves to "
        "NEVER (auto-update disabled) -- set AUTO to leave opencode's auto-update enabled. An explicit "
        "`autoupdate` key in `config_overrides` always wins.",
    )
    # emit_common_transcript gates the raw -> common-schema converter that writes
    # events/opencode/common_transcript/events.jsonl. The raw transcript at
    # logs/opencode_transcript/events.jsonl is always captured (by the in-process
    # plugin); only the converter is gated by this flag.
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit the common transcript that `mngr transcript` reads.",
    )
    preserve_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
    )


class OpenCodeAgent(
    BaseAgent[OpenCodeAgentConfig],
    InteractiveAgentMixin,
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionPreservationMixin,
    HasSessionAdoptionMixin,
    HasUnattendedModeMixin,
    HasPermissionPolicyMixin,
    HasAutoInstallMixin,
):
    """Agent implementation for OpenCode (driven via its server, not TUI keystrokes)."""

    # How long send_message waits for the launch script to have written the
    # server port / root-session files (written at launch, before readiness, so
    # this only ever waits on the first send racing a just-started agent).
    # ClassVars so a test subclass can shrink them.
    _SEND_FILE_WAIT_SECONDS: ClassVar[float] = 30.0
    _SEND_FILE_POLL_INTERVAL_SECONDS: ClassVar[float] = 0.5

    def get_expected_process_name(self) -> str:
        # Both `opencode serve` and `opencode attach` report `opencode`; the
        # attach client is the pane's foreground process (lifecycle detection
        # also matches it among pane descendants).
        return "opencode"

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get lifecycle state, accounting for the ``permissions_waiting`` marker.

        The lifecycle plugin touches ``permissions_waiting`` while opencode is
        blocked on a tool-approval prompt (its ``ask`` permission policy) and clears
        it once the prompt is answered. The base state reads only the ``active``
        marker, which stays present during a prompt (the session is still busy), so
        on its own it would report RUNNING. Promote RUNNING -> WAITING while the
        agent is blocked, since it cannot progress without user intervention. The
        promotion rule lives in ``_resolve_lifecycle_state_for_permission`` so it can
        be unit-tested without a live server.
        """
        base_state = super().get_lifecycle_state()
        is_blocked_on_permission = self._check_file_exists(self._get_agent_dir() / PERMISSIONS_WAITING_FILENAME)
        return _resolve_lifecycle_state_for_permission(base_state, is_blocked_on_permission)

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Start the agent and, on creation, wait for the launch script's readiness sentinel.

        ``opencode_launch.sh`` writes ``READY_SENTINEL_FILENAME`` once the server is
        up and the session exists -- i.e. the agent can accept messages (delivered
        over the HTTP API). Polling that sentinel is a real signal from the launch
        script, replacing the flakier approach of scraping the attach client's TUI
        footer. The sentinel is cleared by the launch script before each (re)start,
        so a stale one can't make this return early.
        """
        super().wait_for_ready_signal(is_creating, start_action, timeout)
        if not is_creating:
            return
        effective_timeout = timeout if timeout is not None else _READY_TIMEOUT_SECONDS
        sentinel_path = self._get_agent_dir() / READY_SENTINEL_FILENAME
        if poll_until(
            lambda: self._check_file_exists(sentinel_path),
            timeout=effective_timeout,
            poll_interval=_READY_POLL_INTERVAL_SECONDS,
        ):
            return
        pane_content = self._capture_pane_content(self.tmux_target)
        raise AgentStartError(
            str(self.name),
            f"OpenCode did not signal readiness within {effective_timeout:.0f}s "
            "(did `opencode serve` fail to start? check the agent's logs/opencode_server.log)"
            + (f"\nPane content:\n{pane_content}" if pane_content else ""),
        )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return no commands/ scripts: both transcripts are written in-process.

        OpenCode has no native JSONL session file to tail, so the in-process
        plugin (``mngr_opencode_plugin.ts``, provisioned into the config dir, not
        commands/) writes the raw transcript -- and, on session idle, the common
        transcript too -- itself. There is therefore no commands/ streamer or
        converter script, but raw capture is still always provisioned (the plugin
        is written unconditionally in ``provision``), satisfying the
        :class:`HasTranscriptMixin` "raw is the source of truth" contract.
        """
        return {}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return no commands/ scripts: the common transcript is emitted in-process.

        The lifecycle plugin rebuilds the common transcript from in-memory state
        on session idle (gated on ``EMIT_COMMON_ENV_VAR``), so there is no
        backgrounded converter to provision -- just the plugin.
        """
        return {}

    def _get_opencode_config_dir(self) -> Path:
        """Per-agent OpenCode config dir (the ``OPENCODE_CONFIG_DIR`` value)."""
        return get_opencode_config_dir(self._get_agent_dir())

    def _get_opencode_data_home(self) -> Path:
        """Per-agent OpenCode data root (the ``XDG_DATA_HOME`` value)."""
        return get_opencode_data_home(self._get_agent_dir())

    def _get_root_session_file_path(self) -> Path:
        """File where the launch script records the root session id (read by send_message)."""
        return get_opencode_root_session_file_path(self._get_agent_dir())

    def _get_server_port_file_path(self) -> Path:
        """File where the launch script records the server's bound port (read by send_message)."""
        return get_opencode_server_port_file_path(self._get_agent_dir())

    def send_message(self, message: str) -> None:
        """Deliver a message by POSTing it to the agent's OpenCode server.

        The attached TUI client renders the prompt and reply, so the message is
        visible in ``mngr connect`` -- without typing into the TUI (which would
        race OpenCode's post-launch input repaint). The prompt is enqueued via
        ``prompt_async`` and the lifecycle marker tracks completion.

        We deliberately do NOT poll the marker afterwards to confirm the turn
        actually started: ``curl -fsS`` already fails loudly if the POST is dropped
        or the server rejects it (the real, observed failure), and an
        accepted-but-never-started turn is not a demonstrated failure mode here.
        Revisit (poll the active marker for a turn-start ACK, as mngr_pi does) if
        that case ever shows up.
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            port = self._read_launch_file(self._get_server_port_file_path(), "server port")
            session_id = self._read_launch_file(self._get_root_session_file_path(), "root session id")
            self._post_prompt(port, session_id, message)

    def _read_launch_file(self, path: Path, description: str) -> str:
        """Read a non-empty value the launch script wrote, briefly waiting for it to appear."""
        value, _, _ = poll_for_value(
            lambda: self._try_read_nonempty_file(path),
            timeout=self._SEND_FILE_WAIT_SECONDS,
            poll_interval=self._SEND_FILE_POLL_INTERVAL_SECONDS,
        )
        if value is None:
            raise SendMessageError(
                str(self.name),
                f"OpenCode {description} file {path} not available; the agent's server may not have started.",
            )
        return value

    def _try_read_nonempty_file(self, path: Path) -> str | None:
        """Return the stripped contents of ``path`` on the host, or None if absent/empty."""
        try:
            content = self.host.read_text_file(path)
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped or None

    def _post_prompt(self, port: str, session_id: str, message: str) -> None:
        """POST ``message`` to the agent's server via ``curl`` on the host (prompt_async)."""
        command = _build_prompt_post_command(port, session_id, message)
        result = self.host.execute_stateful_command(command)
        if not result.success:
            raise SendMessageError(
                str(self.name),
                f"Failed to POST message to the OpenCode server (port {port}, session {session_id}): "
                f"{result.stderr or result.stdout}",
            )

    def _resolve_host_home(self, host: OnlineHostInterface) -> Path:
        """Resolve the host user's real ``$HOME`` over the host shell (works remotely).

        Read from the host (not local ``Path.home()``) so the shared
        ``auth.json`` / global-config source paths are correct on remote hosts.
        On the (essentially never) chance the query fails, exit cleanly via
        ``SystemExit`` -- ``provision`` runs inside ``provision_agent``'s
        ``ConcurrencyExceptionGroup``, which re-raises ``BaseException`` unwrapped
        but wraps plain ``Exception`` into a noisy traceback.
        """
        result = host.execute_idempotent_command('printf %s "$HOME"', timeout_seconds=10.0)
        home = result.stdout.strip()
        if not result.success or not home:
            logger.error(
                "Could not resolve the host's $HOME for opencode provisioning "
                "(exit_success={}, stdout={!r}). Cannot build the per-agent config/data dirs.",
                result.success,
                result.stdout,
            )
            raise SystemExit(1)
        return Path(home)

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Provision the per-agent config dir, lifecycle plugin, auth, and launch script.

        Steps:

        1. Resolve the host user's real ``$HOME`` (shared-auth / global-config source).
        2. Write the per-agent ``opencode.json`` and the lifecycle plugin (which
           writes both the raw and common transcripts in-process) into the config dir.
        3. Point the per-agent ``auth.json`` at the shared host auth (symlink or copy).
        4. Install the launch orchestrator under ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        if self.agent_config.check_installation:
            ensure_cli_installed(host, mngr_ctx, self.get_install_binary_name(), self.get_install_command())
            if self.agent_config.version is not None:
                verify_pinned_cli_version(
                    host,
                    command=str(self.agent_config.command),
                    binary_name=self.get_install_binary_name(),
                    pinned_version=self.agent_config.version,
                )
        host_home = self._resolve_host_home(host)
        self._provision_opencode_config(host, host_home)
        self._provision_plugin(host)
        self._provision_auth(host, host_home)
        with mngr_ctx.concurrency_group.make_concurrency_group("opencode_provisioning") as concurrency_group:
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {LAUNCH_SCRIPT_NAME: _load_opencode_resource(LAUNCH_SCRIPT_NAME)},
                concurrency_group,
            )

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt a session after provisioning so the agent's opencode resumes existing context."""
        self.adopt_session(host, options, mngr_ctx)

    def adopt_session(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt existing OpenCode session(s) so the new agent resumes that conversation.

        Delegates to :func:`~imbue.mngr.api.preservation.adopt_sessions`, which copies every
        ``--adopt`` session (``copy_explicit``) and the ``--from`` clone (``copy_clone``) into
        this agent, then resumes one (``resume``): the clone when ``--from`` is given, otherwise
        the last ``--adopt`` value. The rest stay available in the agent's session switcher.

        * ``--adopt`` (``options.adopt_session``, a tuple): each arg is a ``ses_...`` id (resolved
          across the user-native db and every live/preserved mngr agent's db) or an absolute path
          to a source ``opencode.db``. OpenCode's store is a single ``opencode.db``, so the *first*
          adopted session seeds the staging db (its trio is copied) and each *subsequent* one is
          **merged** into it (its session + descendant rows folded in), rather than overwriting.
        * ``--from <agent>`` (``options.source_agent_state_location``): a generic clone copying the
          source workspace but not its state dir; the source's native opencode store is brought in
          and its lone root session resumed.

        The agent's ``opencode.db`` is built entirely on a LOCAL staging path: the first source db is
        copied there, subsequent sources are merged in, and every adopted session is rebound to this
        agent's work dir -- all via the stdlib ``sqlite3`` module, so the destination host needs no
        ``sqlite3`` CLI. The finished trio is copied onto the (possibly remote) host once, after every
        session has been folded in: the staging db lives for the whole adoption, so the per-callback
        copy-vs-merge decision keys off whether it exists yet (the first session creates it; later ones
        merge). The work dir is resolved once and every adopted session is rebound to it. ``resume``
        writes ``root_session_id`` to the session actually resumed (the clone, else the last ``--adopt``).
        """
        if not options.adopt_session and options.source_agent_state_location is None:
            return
        with tempfile.TemporaryDirectory(prefix="mngr_opencode_adopt_") as staging_root:
            staging_db = Path(staging_root) / "opencode.db"
            new_directory = self._resolve_work_dir_on_host()
            adopt_sessions(
                options.adopt_session,
                options.source_agent_state_location,
                copy_explicit=lambda arg: self._stage_explicit_session(mngr_ctx, staging_db, new_directory, arg),
                copy_clone=lambda location: self._stage_cloned_session(staging_db, new_directory, location),
                resume=lambda session_id: self._point_resume_at_session(host, session_id),
            )
            if staging_db.exists():
                self._push_staging_db(host, staging_db)

    def _stage_explicit_session(self, mngr_ctx: MngrContext, staging_db: Path, new_directory: Path, arg: str) -> str:
        """Resolve one ``--adopt`` value into the local staging db, rebind it, and return its id."""
        with log_span("Adopting OpenCode session from {}", arg):
            session_id, source_db = resolve_adopt_session_db(arg, _adopt_search_db_paths(mngr_ctx))
            self._add_session_to_staging_db(staging_db, source_db, session_id)
            apply_opencode_rebind(staging_db, session_id, new_directory)
        return session_id

    def _stage_cloned_session(
        self, staging_db: Path, new_directory: Path, source_location: HostLocation
    ) -> str | None:
        """Stage the source agent's conversation into the local staging db after a ``--from <agent>`` clone.

        A ``--from`` clone copies the source workspace but not its state dir, so the source's native
        opencode store (``opencode.db`` + ``-wal``/``-shm``) is brought in. The source db is localized
        (pulled to a local staging copy when the source host is remote, used in place when local), its
        lone root session id is read, and it is seeded into / merged into the local staging db just like
        an explicit adopt, then rebound to this agent's work dir. The returned id is what the caller
        resumes.

        A ``--from`` clone is a workspace clone, so carrying the session forward is a bonus: a source
        with no store warns and returns ``None`` (the agent starts fresh) rather than failing.
        """
        source_db = source_location.path / AGENT_OPENCODE_DB_RELPATH
        if not source_location.host.path_exists(source_db):
            logger.warning(
                "Clone adopt: no OpenCode session store at source {}; starting fresh.",
                source_location.path / AGENT_OPENCODE_STORE_RELPATH,
            )
            return None
        local_source_db = self._localize_source_db(source_location, staging_db.parent)
        session_id = read_only_root_session_id(local_source_db)
        with log_span("Adopting cloned OpenCode session {}", session_id):
            self._add_session_to_staging_db(staging_db, local_source_db, session_id)
            apply_opencode_rebind(staging_db, session_id, new_directory)
        return session_id

    def _localize_source_db(self, source_location: HostLocation, staging_root: Path) -> Path:
        """Return a LOCAL path to the clone source's ``opencode.db`` trio (pulling it when the source is remote).

        The merge/rebind run via the stdlib ``sqlite3`` module against a local file, so a remote source
        store is pulled to ``staging_root`` first (the db plus any ``-wal``/``-shm`` sidecars, read over
        the host file interface). A local source is read in place.
        """
        source_db = source_location.path / AGENT_OPENCODE_DB_RELPATH
        if source_location.host.is_local:
            return source_db
        pulled_db = staging_root / "clone_source.db"
        pulled_db.write_bytes(source_location.host.read_file(source_db))
        for suffix in _DB_SIDECAR_SUFFIXES:
            sidecar = source_db.parent / f"{source_db.name}{suffix}"
            if source_location.host.path_exists(sidecar):
                (staging_root / f"{pulled_db.name}{suffix}").write_bytes(source_location.host.read_file(sidecar))
        return pulled_db

    def _add_session_to_staging_db(self, staging_db: Path, source_db: Path, session_id: str) -> None:
        """Seed the local staging db from ``source_db`` (first session) or merge ``session_id`` into it (later).

        OpenCode's store is a single ``opencode.db``, so the first adopted session copies the source trio
        wholesale and each later one is folded in with :func:`apply_opencode_merge` (a file copy would
        clobber the earlier session). Both run locally via the stdlib ``sqlite3`` module.
        """
        if staging_db.exists():
            apply_opencode_merge(staging_db, source_db, session_id)
            return
        shutil.copyfile(source_db, staging_db)
        for suffix in _DB_SIDECAR_SUFFIXES:
            sidecar = source_db.parent / f"{source_db.name}{suffix}"
            if sidecar.exists():
                shutil.copyfile(sidecar, staging_db.parent / f"{staging_db.name}{suffix}")

    def _push_staging_db(self, host: OnlineHostInterface, staging_db: Path) -> None:
        """Copy the finished local staging db trio onto the (possibly remote) host's opencode data dir.

        Run once, after every adopted session has been folded in and rebound, so the host only ever
        receives a complete db -- no host ``sqlite3`` CLI is involved at any point. ``--checksum`` forces
        rsync to compare file content rather than size+mtime, so any pre-existing db at the destination is
        replaced regardless of a coincidentally-matching size and sub-second mtime.
        """
        dest_app_dir = get_opencode_app_data_dir(self._get_opencode_data_home())
        db_include = "--checksum --include=opencode.db --include=opencode.db-wal --include=opencode.db-shm --exclude=*"
        host.copy_local_directory(staging_db.parent, dest_app_dir, db_include)

    def _point_resume_at_session(self, host: OnlineHostInterface, session_id: str) -> None:
        """Write ``session_id`` into ``root_session_id`` so the launch script resumes it (not a fresh one)."""
        host.write_text_file(self._get_root_session_file_path(), session_id)
        logger.info("Resuming OpenCode session {} in agent {}", session_id, self.name)

    def _resolve_work_dir_on_host(self) -> Path:
        """Return ``self.work_dir`` with symlinks resolved as the host sees it.

        OpenCode stores the *resolved* absolute path in its ``session``/``project`` rows (e.g.
        ``/tmp`` -> ``/private/tmp`` on macOS, the volume target on Modal), so the rebind must
        target the resolved form or the directory still won't match. Falls back to the
        unresolved path if ``readlink -f`` fails (warning), as the claude adopt path does.
        """
        result = self.host.execute_idempotent_command(
            f"readlink -f {shlex.quote(str(self.work_dir))}", timeout_seconds=5.0
        )
        if result.success and result.stdout.strip():
            return Path(result.stdout.strip())
        logger.warning(
            "readlink -f {} failed (success={}, stderr={!r}); falling back to unresolved path for the adopt rebind",
            self.work_dir,
            result.success,
            result.stderr.strip(),
        )
        return self.work_dir

    def _provision_opencode_config(self, host: OnlineHostInterface, host_home: Path) -> None:
        """Write the per-agent ``opencode.json`` (idempotent each provision)."""
        base_config: dict[str, Any] = {}
        if self.agent_config.sync_global_config:
            user_config_path = host_home.joinpath(*_USER_CONFIG_RELATIVE_PATH)
            base_config = read_opencode_config(host, user_config_path)
        # Unattended is keyed off the host, matching the other agent plugins.
        disable_auto_update = is_self_update_disabled(self.agent_config.update_policy, is_unattended=not host.is_local)
        per_agent_config = build_opencode_config(
            base_config,
            self.agent_config.config_overrides,
            self.is_unattended_enabled(),
            disable_auto_update=disable_auto_update,
        )
        config_path = get_opencode_config_file_path(self._get_opencode_config_dir())
        with log_span("Writing per-agent opencode config to {}", config_path):
            host.write_text_file(config_path, serialize_opencode_config(per_agent_config))

    def _provision_plugin(self, host: OnlineHostInterface) -> None:
        """Write the lifecycle plugin into the per-agent config dir's ``plugin/``.

        OpenCode auto-loads ``$OPENCODE_CONFIG_DIR/plugin/*.ts`` (verified live),
        so no ``plugin`` entry in opencode.json is needed.
        """
        plugin_path = get_opencode_plugin_path(self._get_opencode_config_dir())
        with log_span("Installing opencode lifecycle plugin at {}", plugin_path):
            host.write_text_file(plugin_path, _load_opencode_resource(PLUGIN_FILENAME))

    def _provision_auth(self, host: OnlineHostInterface, host_home: Path) -> None:
        """Point the per-agent ``auth.json`` at the shared host auth (symlink or copy).

        Symlink mode (default): the per-agent ``auth.json`` symlinks to the shared
        ``~/.local/share/opencode/auth.json`` -- created even if the shared file
        doesn't exist yet. OpenCode writes auth.json in place, so the first
        agent's login writes through to the shared path, authenticating every
        agent. Copy mode copies the shared file in only if it exists, else leaves
        the agent to run OpenCode's login flow.
        """
        source = get_shared_opencode_auth_path(host_home)
        dest = get_opencode_auth_path_for_data_home(self._get_opencode_data_home())
        if self.agent_config.symlink_auth:
            symlink_on_host(host, source, dest, ensure_source_parent=True)
            return
        if not copy_on_host(host, source, dest):
            logger.info(
                "No shared OpenCode auth at {} to copy (symlink_auth=False); the agent will run "
                "OpenCode's login flow on first launch.",
                source,
            )

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Build the launch command: ``env <isolation + MNGR_OPENCODE_*> bash opencode_launch.sh <user-args>``.

        The launch orchestrator (see the resource) starts ``opencode serve``
        (asking for an ephemeral port via ``--port 0`` and recording the actual
        bound port, so co-resident agents never collide), pre-creates/reuses the
        session, and attaches the TUI client in the foreground. The env carries the
        config / data isolation (inherited by both serve and attach), the bin and
        work dir the script needs, and -- when ``emit_common_transcript`` is on --
        ``MNGR_OPENCODE_EMIT_COMMON``, which tells the in-process plugin to emit
        the common transcript on idle. User ``cli_args`` / ``agent_args`` are
        forwarded (shell-quoted) to the attach client.

        Session resume across stop/start is handled inside the script (it reuses
        the recorded root session id), so there is no resume flag here. There is no
        backgrounded supervisor: both transcripts are written in-process by the
        plugin.
        """
        opencode_bin = str(command_override) if command_override is not None else str(self.agent_config.command)
        forwarded_args = " ".join(shlex.quote(arg) for arg in (*self.agent_config.cli_args, *agent_args))

        config_dir = self._get_opencode_config_dir()
        data_home = self._get_opencode_data_home()
        launch_script = "$MNGR_AGENT_STATE_DIR/commands/" + LAUNCH_SCRIPT_NAME
        # The launch script puts this straight into the session-create URL query
        # (?directory=...), so URL-encode it here (in Python, via the stdlib)
        # rather than hand-rolling an encoder in bash. ``safe="/"`` keeps path
        # separators readable; spaces and other URL-significant chars are escaped.
        directory_query = urllib.parse.quote(str(self.work_dir), safe="/")

        env_prefix = (
            f"env {_OPENCODE_CONFIG_DIR_ENV_VAR}={shlex.quote(str(config_dir))}"
            f" {_XDG_DATA_HOME_ENV_VAR}={shlex.quote(str(data_home))}"
            f" {OPENCODE_BIN_ENV_VAR}={shlex.quote(opencode_bin)}"
            f" {OPENCODE_PORT_ENV_VAR}={_EPHEMERAL_PORT}"
            f" {OPENCODE_WORKDIR_ENV_VAR}={shlex.quote(directory_query)}"
        )
        if self.is_common_transcript_enabled:
            env_prefix = f"{env_prefix} {EMIT_COMMON_ENV_VAR}={EMIT_COMMON_ENABLED_VALUE}"

        launch_command = f"{env_prefix} bash {launch_script}"
        if forwarded_args:
            launch_command = f"{launch_command} {forwarded_args}"
        return CommandString(launch_command)

    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        preserve_agent_state(_opencode_preserved_items(), self, host)

    def is_unattended_enabled(self) -> bool:
        return self.agent_config.auto_allow_permissions

    def get_permission_policy(self) -> Mapping[str, Any]:
        # opencode's per-resource policy lives in the `permission` config-overrides key.
        policy = self.agent_config.config_overrides.get("permission", {})
        return policy if isinstance(policy, Mapping) else {}

    def get_install_binary_name(self) -> str:
        return "opencode"

    def get_install_command(self) -> str:
        # The opencode installer reads ``requested_version=${VERSION:-}``, so a pinned
        # version is passed by setting VERSION on the bash that runs the piped script.
        version = self.agent_config.version
        if version is None:
            return "curl -fsSL https://opencode.ai/install | bash"
        return f"curl -fsSL https://opencode.ai/install | VERSION={shlex.quote(version)} bash"

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Preserve transcripts and session-id history before the state dir is deleted."""
        if self.agent_config.preserve_on_destroy:
            self.preserve_session_state(host)


def _opencode_preserved_items() -> list[PreservedItem]:
    """Return the files to preserve from an opencode agent's state directory.

    The raw and common transcripts, the root session-id history, and opencode's
    native resumable session store (the SQLite ``opencode.db`` plus its ``-wal``/``-shm``
    WAL sidecars, and ``storage/``) so the session can be resumed/adopted. The native
    store is targeted by those specific paths so the sibling ``auth.json`` (a symlink to
    shared creds) and ``log/`` are excluded. The ``-wal``/``-shm`` sidecars carry writes
    not yet checkpointed into the main db; preservation skips them when absent (e.g. once
    checkpointed by a clean shutdown), as it does any missing item.
    """
    return [
        *build_transcript_preserved_items("opencode"),
        PreservedItem(rel_path=ROOT_SESSION_FILENAME, kind=FileType.FILE),
        PreservedItem(rel_path=NATIVE_DB_RELATIVE_PATH, kind=FileType.FILE),
        PreservedItem(rel_path=NATIVE_DB_WAL_RELATIVE_PATH, kind=FileType.FILE),
        PreservedItem(rel_path=NATIVE_DB_SHM_RELATIVE_PATH, kind=FileType.FILE),
        PreservedItem(rel_path=NATIVE_STORAGE_RELATIVE_PATH, kind=FileType.DIRECTORY),
    ]


def _opencode_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    """Return the items to preserve for a discovered (offline) opencode agent, or None to skip it."""
    return flag_gated_items(ref, "preserve_on_destroy", _opencode_preserved_items())


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve opencode transcripts from the host's volume before it is destroyed.

    Mirrors ``OpenCodeAgent.on_destroy`` for the offline path, where a host is
    destroyed without per-agent ``on_destroy`` calls but agent state still lives
    on the host's persisted volume.
    """
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName("opencode"), _opencode_items_to_preserve_for_discovered_agent
    )


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Fail-fast pre-resolution of opencode ``--adopt`` session ids (see ``run_adopt_session_preflight``)."""
    run_adopt_session_preflight(
        args.agent_options.agent_type,
        args.agent_options.adopt_session,
        mngr_ctx,
        OpenCodeAgent,
        lambda session_arg: resolve_adopt_session_db(session_arg, _adopt_search_db_paths(mngr_ctx)),
    )
    return None


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the opencode agent type."""
    return ("opencode", OpenCodeAgent, OpenCodeAgentConfig)


def _resolve_lifecycle_state_for_permission(
    base_state: AgentLifecycleState, is_blocked_on_permission: bool
) -> AgentLifecycleState:
    """Layer the ``permissions_waiting`` signal onto the base lifecycle state.

    Promotes RUNNING -> WAITING while opencode is blocked on a tool-approval prompt
    (the base state, which reads only the ``active`` marker, would otherwise report
    RUNNING since the session stays busy). Every non-RUNNING base state passes
    through unchanged. Kept pure (no agent/host) so ``get_lifecycle_state``'s
    promotion rule is unit-testable without standing up a live server.

    Defers the gating decision to the shared ``classify_waiting_reason``: a RUNNING
    base state means the ``active`` marker is present and the process is alive, so
    the classifier's ``is_active`` gate is satisfied and a PERMISSIONS verdict is
    what promotes RUNNING to WAITING. Sharing that one function keeps this promotion
    and the ``waiting_reason`` field generator from drifting apart.
    """
    if base_state != AgentLifecycleState.RUNNING:
        return base_state
    reason = classify_waiting_reason(is_active=True, is_blocked_on_permission=is_blocked_on_permission)
    return AgentLifecycleState.WAITING if reason is WaitingReason.PERMISSIONS else base_state


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting based on marker files, or None.

    Reads the agent state directory's marker files directly rather than calling
    get_lifecycle_state() (which runs tmux/ps SSH commands), then delegates the
    decision to the shared ``classify_waiting_reason`` so this and the lifecycle
    promotion stay in lockstep. The markers are maintained by the in-process
    lifecycle plugin (mngr_opencode_plugin.ts). ``permissions_waiting`` is only read
    when ``active`` is present, both to short-circuit the idle case and because the
    classifier ignores the permission signal when the agent is not in a turn.

    Unlike codex, opencode has no cancelled-dialog ambiguity here: a denied prompt
    emits ``permission.replied`` and a cancelled turn emits ``session.idle``, both of
    which clear the marker promptly (verified live), so ``permissions_waiting`` does
    not strand alongside ``active``.
    """
    agent_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    is_active = host.path_exists(agent_dir / ACTIVE_MARKER_FILENAME)
    is_blocked_on_permission = is_active and host.path_exists(agent_dir / PERMISSIONS_WAITING_FILENAME)
    return classify_waiting_reason(is_active, is_blocked_on_permission)


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose opencode-specific agent fields for listing."""
    return ("opencode", {"waiting_reason": _waiting_reason})
