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
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.common import copy_on_host
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_opencode import resources as _opencode_resources
from imbue.mngr_opencode.opencode_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_opencode.opencode_config import OPENCODE_BIN_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_PORT_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_WORKDIR_ENV_VAR
from imbue.mngr_opencode.opencode_config import PLUGIN_FILENAME
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import compute_server_port
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_opencode_root_session_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_server_port_file_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.opencode_config import read_opencode_config
from imbue.mngr_opencode.opencode_config import serialize_opencode_config

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "opencode_common_transcript.sh"

# Supervisor provisioned into commands/; owns the lifecycle of the common
# transcript converter (the raw transcript is written in-process by the plugin).
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "opencode_background_tasks.sh"

# User's global OpenCode config, the base for the per-agent opencode.json when
# ``sync_global_config`` is set. Lives under the default XDG config dir; honoring
# a custom ``$XDG_CONFIG_HOME`` is a possible future refinement.
_USER_CONFIG_RELATIVE_PATH: Final[tuple[str, ...]] = (".config", "opencode", "opencode.json")

# OpenCode env vars that isolate config and data per agent.
_OPENCODE_CONFIG_DIR_ENV_VAR: Final[str] = "OPENCODE_CONFIG_DIR"
_XDG_DATA_HOME_ENV_VAR: Final[str] = "XDG_DATA_HOME"

# Stable footer-hint substring OpenCode renders only once the input prompt is
# drawn and ready. Deliberately not the ASCII-art splash banner (renders before
# the input row exists). Verified against the live TUI / attach client.
_TUI_READY_INDICATOR: Final[str] = "ctrl+p commands"

# OpenCode server endpoint that enqueues a prompt without blocking on the reply
# (the agent's lifecycle marker tracks completion, so send is fire-and-forget).
_PROMPT_ENDPOINT_TEMPLATE: Final[str] = "http://127.0.0.1:{port}/session/{session_id}/prompt_async"


def _build_prompt_post_command(port: str, session_id: str, message: str) -> str:
    """Build the host ``curl`` command that POSTs ``message`` to the agent's server.

    The message is sent as a JSON text part (so newlines/quotes are carried
    safely) and both the URL and JSON body are shell-quoted.
    """
    url = _PROMPT_ENDPOINT_TEMPLATE.format(port=port, session_id=session_id)
    payload = serialize_opencode_config({"parts": [{"type": "text", "text": message}]})
    return f"curl -sf -X POST {shlex.quote(url)} -H 'content-type: application/json' -d {shlex.quote(payload)}"


def _load_opencode_resource(filename: str) -> str:
    """Load a resource file from the mngr_opencode resources package."""
    resource_files = importlib.resources.files(_opencode_resources)
    return resource_files.joinpath(filename).read_text()


class OpenCodeAgentConfig(AgentTypeConfig):
    """Config for the opencode agent type."""

    command: CommandString = Field(
        default=CommandString("opencode"),
        description="Command to run the opencode agent.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments forwarded to the opencode attach (TUI) client.",
    )
    # config_overrides mirrors mngr_antigravity's settings_overrides: a free-form
    # blob merged last into the per-agent opencode.json. Covers ``model``
    # ("provider/model"), the ``permission`` policy block ({"bash": {"git *":
    # "allow", "rm -rf *": "deny"}, "edit": "ask", ...}), ``small_model``, etc.
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged last into the per-agent opencode.json. "
        'Common keys: model ("provider/model"), permission ({"bash": {...}, "edit": "ask"}). '
        'Example: {"model": "anthropic/claude-sonnet-4-5", "permission": {"bash": {"rm -rf *": "deny"}}}.',
    )
    # sync_global_config mirrors mngr_antigravity's sync_home_settings: when True
    # (default), the per-agent opencode.json starts from a copy of the user's real
    # ~/.config/opencode/opencode.json; config_overrides layer on top. When False,
    # the base is an empty config.
    sync_global_config: bool = Field(
        default=True,
        description="Whether to base the per-agent opencode.json on a copy of the user's real "
        "~/.config/opencode/opencode.json (True, default) or start from an empty base (False).",
    )
    # symlink_auth mirrors mngr_antigravity's symlink_oauth_token. With the
    # default (symlink), the per-agent auth.json symlinks to the shared
    # ~/.local/share/opencode/auth.json so one agent's login authenticates all
    # agents (and refreshes propagate). Copy mode (False) gives full isolation.
    symlink_auth: bool = Field(
        default=True,
        description="Symlink (True, default) each per-agent auth.json to the shared "
        "~/.local/share/opencode/auth.json, so one agent's login authenticates all agents. "
        "Copy (False) for full isolation (no sharing).",
    )
    # auto_allow_permissions injects a wildcard ``permission`` allow into the
    # per-agent opencode.json (auto-approve every action not explicitly denied) --
    # the config analog of OpenCode's ``run --dangerously-skip-permissions``.
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, auto-approve every action not explicitly denied "
        "(injects a wildcard allow into the opencode.json permission block).",
    )
    # emit_common_transcript gates the raw -> common-schema converter that writes
    # events/opencode/common_transcript/events.jsonl. The raw transcript at
    # logs/opencode_transcript/events.jsonl is always captured (by the in-process
    # plugin); only the converter is gated by this flag.
    emit_common_transcript: bool = Field(
        default=True,
        description="When True, emit a common-schema transcript that `mngr transcript` reads.",
    )


class OpenCodeAgent(BaseAgent[OpenCodeAgentConfig], HasCommonTranscriptMixin):
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

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Start the agent and, on creation, wait for the attach client's input row to render.

        OpenCode has no readiness sentinel event, so -- like Antigravity -- we
        poll for a footer string that appears only once the TUI can accept input.
        (Sending does not depend on this; the launch script has the server +
        session ready before the client attaches.)
        """
        super().wait_for_ready_signal(is_creating, start_action, timeout)
        if is_creating:
            wait_for_tui_ready(self, self.tmux_target, _TUI_READY_INDICATOR)

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return no commands/ scripts: the raw transcript is written in-process.

        OpenCode has no native JSONL session file to tail, so the in-process
        plugin (``mngr_opencode_plugin.ts``, provisioned into the config dir, not
        commands/) appends each message/part event to
        ``logs/opencode_transcript/events.jsonl`` itself. Raw capture is therefore
        not a commands/ script -- but it is still always provisioned (the plugin
        is written unconditionally in ``provision``), satisfying the
        :class:`HasTranscriptMixin` "raw is the source of truth" contract.
        """
        return {}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the opencode raw -> common transcript converter."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_opencode_resource(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

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
        race OpenCode's post-launch input repaint). Fire-and-forget: the prompt is
        enqueued via ``prompt_async`` and the lifecycle marker tracks completion.
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
        """Provision the per-agent config dir, lifecycle plugin, auth, and command scripts.

        Steps:

        1. Resolve the host user's real ``$HOME`` (shared-auth / global-config source).
        2. Write the per-agent ``opencode.json`` and the lifecycle plugin into the config dir.
        3. Point the per-agent ``auth.json`` at the shared host auth (symlink or copy).
        4. Install the launch orchestrator, the common-transcript converter, and the
           background supervisor under ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        host_home = self._resolve_host_home(host)
        self._provision_opencode_config(host, host_home)
        self._provision_plugin(host)
        self._provision_auth(host, host_home)
        with mngr_ctx.concurrency_group.make_concurrency_group("opencode_provisioning") as concurrency_group:
            provision_raw_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            maybe_provision_common_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {
                    LAUNCH_SCRIPT_NAME: _load_opencode_resource(LAUNCH_SCRIPT_NAME),
                    _BACKGROUND_TASKS_SCRIPT_NAME: _load_opencode_resource(_BACKGROUND_TASKS_SCRIPT_NAME),
                },
                concurrency_group,
            )

    def _provision_opencode_config(self, host: OnlineHostInterface, host_home: Path) -> None:
        """Write the per-agent ``opencode.json`` (idempotent each provision)."""
        base_config: dict[str, Any] = {}
        if self.agent_config.sync_global_config:
            user_config_path = host_home.joinpath(*_USER_CONFIG_RELATIVE_PATH)
            base_config = read_opencode_config(host, user_config_path)
        per_agent_config = build_opencode_config(
            base_config,
            self.agent_config.config_overrides,
            self.agent_config.auto_allow_permissions,
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

    def _build_background_tasks_command(self) -> str:
        """Shell snippet that backgrounds the transcript-supervisor subshell."""
        script_path = f"$MNGR_AGENT_STATE_DIR/commands/{_BACKGROUND_TASKS_SCRIPT_NAME}"
        return f"( bash {script_path} {shlex.quote(self.session_name)} ) &"

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Build the launch command: backgrounded converter + the serve/attach orchestrator.

        Composition:

        1. ``( bash opencode_background_tasks.sh <session> ) &`` -- backgrounded
           common-transcript converter supervisor (only when ``emit_common_transcript``).
        2. ``env <isolation + MNGR_OPENCODE_*> bash opencode_launch.sh <user-args>``
           -- the launch orchestrator (see the resource): it starts ``opencode
           serve`` on the per-agent port, pre-creates/reuses the session, and
           attaches the TUI client in the foreground. The env carries the config /
           data isolation (inherited by both serve and attach) plus the bin, port,
           and work dir the script needs. User ``cli_args`` / ``agent_args`` are
           forwarded (shell-quoted) to the attach client.

        Session resume across stop/start is handled inside the script (it reuses
        the recorded root session id), so there is no resume flag here.
        """
        opencode_bin = str(command_override) if command_override is not None else str(self.agent_config.command)
        forwarded_args = " ".join(shlex.quote(arg) for arg in (*self.agent_config.cli_args, *agent_args))

        config_dir = self._get_opencode_config_dir()
        data_home = self._get_opencode_data_home()
        port = compute_server_port(str(self.id))
        launch_script = "$MNGR_AGENT_STATE_DIR/commands/" + LAUNCH_SCRIPT_NAME

        env_prefix = (
            f"env {_OPENCODE_CONFIG_DIR_ENV_VAR}={shlex.quote(str(config_dir))}"
            f" {_XDG_DATA_HOME_ENV_VAR}={shlex.quote(str(data_home))}"
            f" {OPENCODE_BIN_ENV_VAR}={shlex.quote(opencode_bin)}"
            f" {OPENCODE_PORT_ENV_VAR}={port}"
            f" {OPENCODE_WORKDIR_ENV_VAR}={shlex.quote(str(self.work_dir))}"
        )
        launch_command = f"{env_prefix} bash {launch_script}"
        if forwarded_args:
            launch_command = f"{launch_command} {forwarded_args}"

        if not self.is_common_transcript_enabled:
            return CommandString(launch_command)
        return CommandString(f"{self._build_background_tasks_command()} {launch_command}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the opencode agent type."""
    return ("opencode", OpenCodeAgent, OpenCodeAgentConfig)
