import importlib.resources
import json
import os
import shlex
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_keystroke
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_pi_coding import resources as _pi_resources

_PI_HOME_DIR_NAME: str = ".pi"
_PI_AGENT_SUBDIR: str = "agent"

# The pi agent-type name, used for the per-agent transcript directories
# (``events/<type>/common_transcript`` and ``logs/<type>_transcript``) and
# passed to the lifecycle extension via ``MNGR_PI_AGENT_TYPE``. Kept in sync
# with the name returned by ``register_agent_type`` and with the default in
# ``mngr_pi_lifecycle.ts``.
_PI_AGENT_TYPE: str = "pi-coding"

# The mngr lifecycle extension (see resources/mngr_pi_lifecycle.ts). Provisioned
# into the agent state dir and loaded with ``pi -e`` so it can maintain the
# ``active`` marker, the readiness sentinel, and the transcripts -- pi has no
# shell-hook surface, so an extension is the only lever for these.
_LIFECYCLE_EXTENSION_NAME: str = "mngr_pi_lifecycle.ts"

# Written by the extension's ``session_start`` handler; polled by
# ``wait_for_ready_signal`` as the "input is ready" signal. Kept in sync with
# ``SESSION_STARTED_SENTINEL_NAME`` in mngr_pi_lifecycle.ts.
_SESSION_STARTED_SENTINEL_NAME: str = "pi_session_started"

# Written by the extension with the main session's file path; read (shell-
# evaluated) by ``assemble_command`` to resume via ``pi --session <file>``. Kept
# in sync with ``SESSION_FILE_NAME`` in mngr_pi_lifecycle.ts.
_SESSION_FILE_NAME: str = "pi_session_file"

# How long to wait for the readiness sentinel at create time. Matches the
# TUI-ready budget in ``tui_utils`` -- startup can be slow on remote hosts that
# must render the TUI before the session loads.
_READY_TIMEOUT_SECONDS: float = 30.0

# Message submission: send Enter, then wait this long for the ``active`` marker
# (the turn starting) before re-sending. pi occasionally swallows the first
# Enter after a paste; retrying makes submission reliable. The marker appears
# within a beat of a real submit, so a swallowed Enter is the only thing that
# burns an attempt.
_SUBMIT_MAX_ATTEMPTS: int = 4
_SUBMIT_PER_ATTEMPT_TIMEOUT_SECONDS: float = 4.0


def _load_resource(filename: str) -> str:
    """Load a resource file (e.g. the lifecycle extension) shipped in the wheel."""
    return importlib.resources.files(_pi_resources).joinpath(filename).read_text()


def _get_pi_home_dir(home_dir: Path | None = None) -> Path:
    """Return the pi agent home directory (defaults to ~/.pi/agent/)."""
    if home_dir is None:
        home_dir = Path.home()
    return home_dir / _PI_HOME_DIR_NAME / _PI_AGENT_SUBDIR


class PiCodingAgentConfig(AgentTypeConfig):
    """Config for the pi-coding agent type."""

    command: CommandString = Field(
        default=CommandString("pi"),
        description="Command to run the pi coding agent",
    )
    sync_home_settings: bool = Field(
        default=True,
        description="Whether to sync settings from ~/.pi/agent/ to the per-agent config dir",
    )
    sync_auth: bool = Field(
        default=True,
        description="Whether to sync the auth.json from ~/.pi/agent/ to the per-agent config dir",
    )
    check_installation: bool = Field(
        default=True,
        description="Check if pi is installed (if False, assumes it is already present)",
    )
    resume_session: bool = Field(
        default=True,
        description=(
            "Resume this agent's pi session on start (via `pi --session <recorded file>`), so "
            "`mngr stop` then `mngr start` keeps conversation context. Safe on first start "
            "(pi starts fresh when there is no recorded session yet)."
        ),
    )
    emit_common_transcript: bool = Field(
        default=True,
        description=(
            "Emit the agent-agnostic common transcript that `mngr transcript` reads. The raw "
            "pi transcript is always captured; this gates only the common-envelope conversion."
        ),
    )
    emit_raw_transcript: bool = Field(
        default=True,
        description="Capture the raw pi message stream under logs/<type>_transcript/events.jsonl.",
    )


def _check_pi_installed(host: OnlineHostInterface) -> bool:
    """Check if pi is installed on the host."""
    result = host.execute_idempotent_command("command -v pi", timeout_seconds=10.0)
    return result.success


# The npm package pi ships under. It migrated from @mariozechner/pi-coding-agent
# (now deprecated and frozen) to the @earendil-works scope, which carries the
# current releases; the binary, config dir (.pi), and PI_CODING_AGENT_DIR env var
# are unchanged across the move.
_PI_NPM_PACKAGE: str = "@earendil-works/pi-coding-agent"


def _install_pi(host: OnlineHostInterface) -> None:
    """Install pi on the host via npm."""
    result = host.execute_idempotent_command(
        f"npm install -g {_PI_NPM_PACKAGE}",
        timeout_seconds=300.0,
    )
    if not result.success:
        raise PluginMngrError(f"Failed to install pi. stderr: {result.stderr}")


def _has_api_credentials_available(
    host: OnlineHostInterface,
    options: CreateAgentOptions,
    home_dir: Path | None = None,
) -> bool:
    """Check whether API credentials appear to be available for pi.

    Pi supports many providers, but the most common is ANTHROPIC_API_KEY.
    Checks environment variables (process env for local hosts, agent env vars,
    host env vars), and the auth.json file.
    """
    api_key_env_vars = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    )

    for key in api_key_env_vars:
        if host.is_local and os.environ.get(key):
            return True
        for env_var in options.environment.env_vars:
            if env_var.key == key:
                return True
        if host.get_env_var(key):
            return True

    auth_path = _get_pi_home_dir(home_dir) / "auth.json"
    if auth_path.exists():
        auth_data = json.loads(auth_path.read_text())
        if auth_data:
            return True

    return False


class PiCodingAgent(InteractiveTuiAgent[PiCodingAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for the pi coding agent with TUI handling.

    pi's only lifecycle-event surface is its TypeScript extension API (no
    shell hooks). mngr therefore provisions a single extension
    (``mngr_pi_lifecycle.ts``) and loads it with ``pi -e``; that extension
    maintains the ``active`` RUNNING/WAITING marker (subagent-aware via a
    root-session id), writes the readiness sentinel this class waits on, and
    emits both the raw and common transcripts. Because emission happens inside
    the extension, the transcript-mixin script hooks return nothing.
    """

    # Required by InteractiveTuiAgent, but pi readiness is gated on the
    # extension's session_start sentinel instead (see wait_for_ready_signal) --
    # the "pi v" banner prints before the session is ready for input. Retained
    # only to satisfy the base-class contract / for diagnostics.
    TUI_READY_INDICATOR = "pi v"

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """No ``commands/`` scripts: the lifecycle extension emits the raw transcript.

        pi exposes structured ``message_end`` events, so the extension writes the
        raw stream directly rather than tailing pi's session JSONL from a
        backgrounded shell streamer (the claude/agy pattern).
        """
        return {}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """No ``commands/`` scripts: the lifecycle extension emits the common transcript."""
        return {}

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        """Submit the pasted message, confirming via the turn actually starting.

        pi exposes no submission hook and no reliable input-cleared placeholder,
        and a single Enter sent right after a paste is sometimes swallowed by the
        TUI -- most often on the first message of a fresh session, before the
        editor has fully absorbed the paste. The dependable signal that the
        message submitted is the lifecycle extension's ``active`` marker: pi fires
        ``agent_start`` (and the extension writes the marker) only once it begins
        processing the turn. So send Enter and poll for the marker, re-sending
        Enter if the turn has not started yet.

        If the marker is already present (a steering message to an agent that is
        already mid-turn), the first poll returns immediately -- we cannot use the
        marker to confirm that case, so the single Enter is best-effort, matching
        the prior behavior.
        """
        marker_path = self._get_agent_dir() / "active"
        for _attempt in range(_SUBMIT_MAX_ATTEMPTS):
            send_enter_keystroke(self, tmux_target)
            if poll_until(
                lambda: self._check_file_exists(marker_path),
                timeout=_SUBMIT_PER_ATTEMPT_TIMEOUT_SECONDS,
                poll_interval=0.3,
            ):
                return
        raise SendMessageError(
            str(self.name),
            f"pi did not start a turn after {_SUBMIT_MAX_ATTEMPTS} Enter attempts "
            f"({_SUBMIT_MAX_ATTEMPTS * _SUBMIT_PER_ATTEMPT_TIMEOUT_SECONDS:.0f}s); the message may not have submitted",
        )

    def get_pi_config_dir(self) -> Path:
        """Return the per-agent pi config directory path.

        This directory replaces ~/.pi/agent/ for this agent when PI_CODING_AGENT_DIR
        is set. Located at $MNGR_AGENT_STATE_DIR/plugin/pi_coding/.
        """
        return self._get_agent_dir() / "plugin" / "pi_coding"

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Isolate pi's config per-agent and hand the lifecycle extension its knobs.

        ``MNGR_AGENT_STATE_DIR`` (where the marker, sentinel, and transcripts
        live) is injected by the host; the extension reads it directly. The
        remaining vars tell the extension which agent-type subdirectory to use
        and whether to emit each transcript layer.
        """
        env_vars["PI_CODING_AGENT_DIR"] = str(self.get_pi_config_dir())
        env_vars["MNGR_PI_AGENT_TYPE"] = _PI_AGENT_TYPE
        env_vars["MNGR_PI_EMIT_COMMON_TRANSCRIPT"] = "1" if self.agent_config.emit_common_transcript else "0"
        env_vars["MNGR_PI_EMIT_RAW_TRANSCRIPT"] = "1" if self.agent_config.emit_raw_transcript else "0"

    def _get_lifecycle_extension_path(self) -> Path:
        """Path the lifecycle extension is provisioned to and loaded from (``pi -e``)."""
        return self._get_agent_dir() / "commands" / _LIFECYCLE_EXTENSION_NAME

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Build the launch command: the base pi invocation plus the mngr extension and resume.

        ``-e <extension>`` loads the lifecycle extension (marker, readiness
        sentinel, transcripts). No background helper is launched, so the
        foreground (and lifecycle-detected) process is plain ``pi``
        (``get_expected_process_name`` pins ``"pi"`` regardless).

        Resume (when ``resume_session``) appends ``--session <file>`` for the
        main session's file recorded by the extension in ``pi_session_file``.
        It is shell-evaluated here because the stored command is replayed on
        every ``mngr start``: a ``set --`` prelude reads the file and adds the
        flag only when it names an existing session, so the first start (no file
        yet) cleanly begins fresh. ``--session <file>`` is preferred over
        ``--continue`` because ``--continue`` resumes the most-recent session for
        the cwd, which a nested pi (run via the bash tool, sharing this agent's
        config dir) can have created -- the recorded file always names *this*
        agent's session. ``set --`` / ``"$@"`` splices the path without
        word-splitting, so a path with spaces survives under both bash and zsh.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        invocation = f"{base_command} -e {shlex.quote(str(self._get_lifecycle_extension_path()))}"
        if not self.agent_config.resume_session:
            return CommandString(invocation)
        quoted_session_file = shlex.quote(str(self._get_agent_dir() / _SESSION_FILE_NAME))
        resume_prelude = (
            f"__mngr_pi_sess=$(cat {quoted_session_file} 2>/dev/null || true); set --; "
            'if [ -n "$__mngr_pi_sess" ] && [ -f "$__mngr_pi_sess" ]; then set -- --session "$__mngr_pi_sess"; fi'
        )
        return CommandString(f'{resume_prelude}; {invocation} "$@"')

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Start the agent and, on creation, wait for the lifecycle extension's sentinel.

        The extension writes ``pi_session_started`` from pi's ``session_start``
        event, which fires once pi has loaded the session and can accept input.
        We wait specifically for that file -- NOT the ``"pi v"`` startup banner,
        which pi prints earlier, before the session has loaded (and before
        first-run setup like downloading ``fd``/``rg`` finishes). Gating on the
        banner would let ``create`` return too early, so the first message sent
        right afterwards lands before pi can process it and is lost. Raises
        ``AgentStartError`` if the sentinel does not appear in time.
        """
        start_action()
        if not is_creating:
            return
        effective_timeout = timeout if timeout is not None else _READY_TIMEOUT_SECONDS
        sentinel_path = self._get_agent_dir() / _SESSION_STARTED_SENTINEL_NAME
        if poll_until(
            lambda: self._check_file_exists(sentinel_path),
            timeout=effective_timeout,
            poll_interval=0.25,
        ):
            return
        pane_content = self._capture_pane_content(self.tmux_target)
        raise AgentStartError(
            str(self.name),
            f"pi did not write its readiness sentinel within {effective_timeout:.1f}s "
            "(is the mngr lifecycle extension loading?)"
            + (f"\nPane content:\n{pane_content}" if pane_content else ""),
        )

    def get_expected_process_name(self) -> str:
        """Return 'pi' as the expected process name.

        Pi sets process.title = "pi" in cli.ts.
        """
        return "pi"

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Validate preconditions before provisioning."""
        if not _has_api_credentials_available(host, options):
            logger.warning(
                "No API credentials detected for pi. The agent may fail to start.\n"
                "Provide credentials via one of:\n"
                "  - Set ANTHROPIC_API_KEY environment variable (use --pass-env ANTHROPIC_API_KEY)\n"
                "  - Run 'pi' and use /login to configure credentials in ~/.pi/agent/auth.json"
            )

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        """No file transfers needed -- provisioning handles config setup directly."""
        return []

    def _setup_per_agent_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        home_dir: Path | None = None,
    ) -> None:
        """Create and populate the per-agent pi config directory.

        This directory is pointed to by PI_CODING_AGENT_DIR so that pi
        uses per-agent config/sessions/state instead of the global ~/.pi/agent/.
        """
        config_dir = self.get_pi_config_dir()

        result = host.execute_idempotent_command(
            f"mkdir -p -m 0700 {shlex.quote(str(config_dir))}", timeout_seconds=5.0
        )
        if not result.success:
            raise PluginMngrError(f"Failed to create per-agent config dir {config_dir}: {result.stderr}")

        if host.is_local:
            self._setup_local_config_dir(host, config, config_dir, home_dir)
        else:
            self._setup_remote_config_dir(host, config, config_dir, home_dir)

    def _setup_local_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        config_dir: Path,
        home_dir: Path | None = None,
    ) -> None:
        """Set up the per-agent config dir on a local host via symlinks."""
        home_pi = _get_pi_home_dir(home_dir)

        if config.sync_auth:
            auth_source = home_pi / "auth.json"
            if auth_source.exists():
                result = host.execute_idempotent_command(
                    f"ln -sf {shlex.quote(str(auth_source))} {shlex.quote(str(config_dir / 'auth.json'))}",
                    timeout_seconds=5.0,
                )
                if not result.success:
                    logger.warning("Failed to symlink auth.json: {}", result.stderr)

        if config.sync_home_settings:
            settings_source = home_pi / "settings.json"
            if settings_source.exists():
                result = host.execute_idempotent_command(
                    f"ln -sf {shlex.quote(str(settings_source))} {shlex.quote(str(config_dir / 'settings.json'))}",
                    timeout_seconds=5.0,
                )
                if not result.success:
                    logger.warning("Failed to symlink settings.json: {}", result.stderr)

            for dir_name in ("skills", "prompts", "extensions", "themes"):
                source = home_pi / dir_name
                if source.exists():
                    result = host.execute_idempotent_command(
                        f"ln -sf {shlex.quote(str(source))} {shlex.quote(str(config_dir / dir_name))}",
                        timeout_seconds=5.0,
                    )
                    if not result.success:
                        logger.warning("Failed to symlink {}: {}", dir_name, result.stderr)

    def _setup_remote_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        config_dir: Path,
        home_dir: Path | None = None,
    ) -> None:
        """Set up the per-agent config dir on a remote host via file copies."""
        home_pi = _get_pi_home_dir(home_dir)

        if config.sync_auth:
            auth_source = home_pi / "auth.json"
            if auth_source.exists():
                logger.info("Transferring auth.json to per-agent config dir...")
                host.write_text_file(config_dir / "auth.json", auth_source.read_text())

        if config.sync_home_settings:
            settings_source = home_pi / "settings.json"
            if settings_source.exists():
                logger.info("Transferring settings.json to per-agent config dir...")
                host.write_text_file(config_dir / "settings.json", settings_source.read_text())

            # Transfer the resource directories with a single rsync rather than
            # one write_file per file. A per-file upload opens an SFTP channel
            # per file (a full round-trip over the SSH tunnel) and does not
            # scale to large resource sets -- see github issue 1825.
            include_args: list[str] = []
            for dir_name in ("skills", "prompts", "extensions", "themes"):
                if (home_pi / dir_name).is_dir():
                    include_args.extend([f"--include={dir_name}/", f"--include={dir_name}/**"])
            if include_args:
                include_args.append("--exclude=*")
                host.copy_local_directory(home_pi, config_dir, " ".join(include_args))

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Provision the per-agent config dir and install pi if needed."""
        config = self.agent_config

        if config.check_installation:
            is_installed = _check_pi_installed(host)
            if is_installed:
                logger.debug("pi is already installed on the host")
            else:
                install_hint = f"npm install -g {_PI_NPM_PACKAGE}"
                if host.is_local and not mngr_ctx.is_auto_approve:
                    raise PluginMngrError(f"pi is not installed. Please install it with:\n  {install_hint}")
                elif not host.is_local and not mngr_ctx.config.is_remote_agent_installation_allowed:
                    raise PluginMngrError(
                        "pi is not installed on the remote host and automatic remote installation is disabled."
                    )
                else:
                    logger.info("Installing pi...")
                    _install_pi(host)
                    logger.info("pi installed successfully")

        self._setup_per_agent_config_dir(host, config)
        self._provision_lifecycle_extension(host)

    def _provision_lifecycle_extension(self, host: OnlineHostInterface) -> None:
        """Write the lifecycle extension into the agent state dir for ``pi -e`` to load.

        Placed under ``commands/`` (not the per-agent ``extensions/`` dir) so pi
        does not *also* auto-discover it -- it must load exactly once, via the
        explicit ``-e`` flag on the mngr-launched process, so that a nested pi
        (which is not given ``-e``) never runs it. ``write_text_file`` creates
        intermediate directories.
        """
        extension_path = self._get_lifecycle_extension_path()
        with log_span("Installing pi lifecycle extension at {}", extension_path):
            host.write_text_file(extension_path, _load_resource(_LIFECYCLE_EXTENSION_NAME))

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """No post-provisioning steps needed."""

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """No extra cleanup needed -- the per-agent config dir is deleted with the agent state."""


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the pi-coding agent type."""
    return ("pi-coding", PiCodingAgent, PiCodingAgentConfig)
