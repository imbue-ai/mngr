import importlib.resources
import json
import os
import shlex
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_pi_coding import resources as _pi_resources

_PI_HOME_DIR_NAME: str = ".pi"
_PI_AGENT_SUBDIR: str = "agent"

# Resource directories under ~/.pi/agent/ shared into each agent's isolated
# config dir when ``sync_home_settings`` is on. ``agents`` holds subagent
# definitions (``*.md``) that subagent extensions (pi's in-tree example, or
# community packages like ``pi-subagents``) read from the config dir; without it
# a synced subagent extension would load but find no agents to delegate to.
#
# Note: the ``npm`` dir (where ``pi install`` materialises npm-package extensions
# under ``npm/node_modules``) is deliberately NOT synced. pi re-resolves the
# ``packages`` list in the synced ``settings.json`` on every startup and
# auto-installs any missing ones into the per-agent ``$PI_CODING_AGENT_DIR/npm``,
# so npm-package extensions (e.g. ``npm:pi-subagents``) become available without
# copying ``node_modules`` around, and each agent keeps an isolated install. The
# only cost is a per-agent ``npm install`` (~1s) on first launch, which needs
# network and so would not work on a fully-offline host; if that latency or the
# offline case ever matters, copy ``npm`` into the per-agent dir here (copy, not
# symlink -- a shared ``node_modules`` would race across concurrent startups).
_SYNCED_RESOURCE_DIRS: tuple[str, ...] = ("skills", "prompts", "extensions", "themes", "agents")

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

# Input delivery: mngr appends one JSON-encoded message per line to this file in
# the agent state dir; the lifecycle extension's watcher injects each new line
# into the live session via pi.sendUserMessage (no tmux keystrokes, TUI stays
# viewable). Kept in sync with INBOX_NAME in mngr_pi_lifecycle.ts.
_INBOX_FILE_NAME: str = "pi_inbox"

# After inboxing a message, wait up to this long for the turn to start (the
# ``active`` marker to appear) as delivery confirmation. Covers the extension's
# poll interval plus pi accepting the injected message.
_TURN_CONFIRM_TIMEOUT_SECONDS: float = 16.0


def _load_resource(filename: str) -> str:
    """Load a resource file (e.g. the lifecycle extension) shipped in the wheel."""
    return importlib.resources.files(_pi_resources).joinpath(filename).read_text()


# pi 0.79+ shows a "Trust project folder?" dialog when there is no saved decision
# and the workspace has "project trust inputs": specifically (pi's
# ``hasProjectTrustInputs``) a ``.pi`` config dir in the cwd, or a ``.agents/skills``
# dir in the cwd or any ancestor. (CLAUDE.md/AGENTS.md do NOT trigger it -- they are
# project context pi loads *once trusted*, not trust triggers; verified on 0.79.1.)
# Decisions are stored per canonical (realpath) cwd in ``<agent-dir>/trust.json`` as
# ``{path: bool}`` (see pi's core/trust-manager.ts). mngr seeds it so the interactive
# agent never stalls at the dialog.
_PI_TRUST_FILE_NAME: str = "trust.json"


def _read_pi_trust(content: str | None, path: Path) -> dict[str, bool]:
    """Parse a pi ``trust.json`` body into ``{canonical_path: bool}``.

    Mirrors pi's own reader: the file is a JSON object of path -> true/false/null.
    ``null`` means "no saved decision" and is dropped. A malformed file is a hard
    error (``UserInputError``) rather than silently overwritten, so a schema we
    don't understand is surfaced instead of clobbered.
    """
    if content is None or content.strip() == "":
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise UserInputError(f"pi trust store at {path} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise UserInputError(f"pi trust store at {path} must be a JSON object, got {type(parsed).__name__}")
    result: dict[str, bool] = {}
    for key, value in parsed.items():
        if value is None:
            continue
        if not isinstance(value, bool):
            raise UserInputError(f"pi trust store at {path}: value for {key!r} must be true, false, or null")
        result[str(key)] = value
    return result


def _serialize_pi_trust(trust: Mapping[str, bool]) -> str:
    """Serialize a trust map in pi's format (sorted keys, 2-space indent, trailing newline)."""
    return json.dumps({key: trust[key] for key in sorted(trust)}, indent=2) + "\n"


def _inbox_append_command(inbox_path: Path, message: str) -> str:
    """Shell command that appends one JSON-encoded message line to the inbox.

    JSON encoding keeps the message on a single line (newlines escaped), so a
    single ``>>`` append is exactly one inbox entry. ``printf '%s\\n'`` writes the
    shell-quoted argument literally followed by a newline.
    """
    return f"printf '%s\\n' {shlex.quote(json.dumps(message))} >> {shlex.quote(str(inbox_path))}"


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
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description=(
            "Trust the agent's workspace for pi without prompting, suppressing pi's "
            "'Trust project folder?' dialog (which would otherwise block the first message). "
            "Also implied by `mngr create --yes`. When False and the source repo is not already "
            "trusted, mngr prompts interactively and refuses to run non-interactively."
        ),
    )


def _check_pi_installed(host: OnlineHostInterface) -> bool:
    """Check if pi is installed on the host."""
    result = host.execute_idempotent_command("command -v pi", timeout_seconds=10.0)
    return result.success


# The npm package pi ships under (used for the remote-host auto-install).
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


class PiCodingAgent(BaseAgent[PiCodingAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for the pi coding agent.

    pi's only lifecycle-event surface is its TypeScript extension API (no
    shell hooks). mngr therefore provisions a single extension
    (``mngr_pi_lifecycle.ts``) and loads it with ``pi -e``; that extension
    maintains the ``active`` RUNNING/WAITING marker, writes the readiness
    sentinel this class waits on, emits
    both the raw and common transcripts, and injects input that mngr appends to
    the agent's inbox. Because emission happens inside the extension, the
    transcript-mixin script hooks return nothing.

    pi runs as an interactive TUI in the agent's tmux session (attach with
    ``mngr connect``), but mngr delivers messages via the extension's
    ``pi.sendUserMessage`` injection rather than tmux keystrokes (see
    ``send_message``), so this subclasses ``BaseAgent`` directly rather than
    ``InteractiveTuiAgent`` -- it uses none of the latter's paste/Enter pipeline.
    """

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

    def send_message(self, message: str) -> None:
        """Deliver a message by appending it to the inbox the lifecycle extension injects.

        pi has no IPC for input, but its extension API injects a user message
        (``pi.sendUserMessage``) into the live session. mngr appends one
        JSON-encoded message per line to ``<agent_dir>/pi_inbox``; the extension's
        watcher injects each new line. This replaces typing into the TUI via tmux
        ``send-keys`` -- which pi intermittently swallowed (the first Enter after a
        paste), forcing retries -- keeps the TUI viewable, and behaves identically
        on local and remote hosts.

        Delivery is confirmed by the turn starting (the ``active`` marker
        appearing), the same signal lifecycle detection uses. If the marker is
        already present (a steering message to an already-running agent), the
        injected message is queued (``deliverAs: followUp``) and we return without
        a marker-based confirmation. The message lock serializes concurrent sends
        so their inbox appends don't interleave.
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._append_to_inbox(message)
            self._confirm_turn_started()

    def _append_to_inbox(self, message: str) -> None:
        """Append one JSON-encoded message line to the agent's inbox on the host.

        The append is stateful (it must run exactly once, never be retried/deduped
        like an idempotent command) and uses ``>>`` so concurrent sends can't lose
        a line.
        """
        inbox_path = self._get_agent_dir() / _INBOX_FILE_NAME
        result = self.host.execute_stateful_command(_inbox_append_command(inbox_path, message))
        if not result.success:
            raise SendMessageError(str(self.name), f"failed to write to pi inbox: {result.stderr or result.stdout}")

    def _confirm_turn_started(self, timeout: float = _TURN_CONFIRM_TIMEOUT_SECONDS) -> None:
        """Wait for the injected message to start a turn (the ``active`` marker appearing)."""
        marker_path = self._get_agent_dir() / "active"
        if self._check_file_exists(marker_path):
            # Already running: the followUp message is queued; no marker-based confirmation.
            return
        if poll_until(
            lambda: self._check_file_exists(marker_path),
            timeout=timeout,
            poll_interval=0.3,
        ):
            return
        raise SendMessageError(
            str(self.name),
            f"pi did not start a turn within {timeout:.0f}s of inboxing the message "
            "(is the lifecycle extension running?)",
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
        We wait specifically for that file. Raises ``AgentStartError`` if the
        sentinel does not appear in time.
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

        # `symlink_on_host` centralizes the shell quoting and uses `ln -sfn` + an mkdir of
        # the link's parent (the shared helper every plugin uses; see hosts/common.py).
        if config.sync_auth:
            # Linked even if it does not exist yet: a `/login` or token refresh inside any
            # agent then writes through to the shared ~/.pi/agent/auth.json and propagates to
            # the rest (the dangling-symlink-before-source case the helper handles).
            symlink_on_host(
                host,
                home_pi / "auth.json",
                config_dir / "auth.json",
                ensure_source_parent=True,
            )

        if config.sync_home_settings:
            # settings + resource dirs are read-shares of the user's existing config: only
            # link what is actually present, rather than fabricating a write-through link.
            settings_source = home_pi / "settings.json"
            if settings_source.exists():
                symlink_on_host(host, settings_source, config_dir / "settings.json")

            for dir_name in _SYNCED_RESOURCE_DIRS:
                source = home_pi / dir_name
                if source.exists():
                    symlink_on_host(host, source, config_dir / dir_name)

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
            for dir_name in _SYNCED_RESOURCE_DIRS:
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

        # Trust gate first (consent + durable global record), so a declined /
        # non-interactive-without-opt-in case exits cleanly before any setup.
        self._ensure_source_repo_trusted(mngr_ctx)
        self._setup_per_agent_config_dir(host, config)
        self._seed_per_agent_workspace_trust(host)
        self._provision_lifecycle_extension(host)

    def _ensure_source_repo_trusted(self, mngr_ctx: MngrContext, home_dir: Path | None = None) -> None:
        """Record the agent's *source repo* as trusted in the user's global pi trust store.

        pi 0.79+ stops an interactive session at a "Trust project folder?" dialog
        when the cwd has a ``.pi`` config dir, or the cwd/an ancestor has a
        ``.agents/skills`` dir, and there is no saved trust decision. mngr seeds
        trust so the agent never stalls there, but -- mirroring
        mngr_claude/mngr_antigravity -- it
        does not silently trust code: trust is split into a *durable* and a
        *transient* record.

        This method handles the **durable** half. The git source-repo root (the
        parent repo of a worktree, or the work_dir for a standalone project) is
        the stable thing worth persisting: once trusted, later agents/worktrees of
        the same repo extend that trust without re-prompting, and a manual ``pi``
        run in the repo is likewise trusted. It is written to the user's global
        ``~/.pi/agent/trust.json``.

        Consent gating: source already trusted -> no-op; ``auto_dismiss_dialogs``
        or ``mngr create --yes`` (``is_auto_approve``) -> silent; interactive ->
        ``click.confirm`` (defaults to no); non-interactive without opt-in, or a
        declined prompt -> ``SystemExit(1)`` (a clean exit, not a traceback).

        The global store is the local user's own config, so it is read and
        written with local filesystem ops regardless of where the agent runs
        (a remote agent still reflects the trust decisions of the user who
        created it). ``home_dir`` is injectable for tests.
        """
        source_path = self._find_git_source_path(mngr_ctx) or self.work_dir
        source_key = str(source_path.resolve())
        trust_path = _get_pi_home_dir(home_dir) / _PI_TRUST_FILE_NAME
        existing = trust_path.read_text() if trust_path.exists() else None
        trust = _read_pi_trust(existing, trust_path)
        if trust.get(source_key) is True:
            logger.debug("pi source repo {} already trusted in {}", source_key, trust_path)
            return

        if not (self.agent_config.auto_dismiss_dialogs or mngr_ctx.is_auto_approve):
            if not mngr_ctx.is_interactive:
                logger.error(
                    "Source directory {} is not trusted by pi. mngr will not silently run an agent on "
                    "untrusted code. Re-run interactively to be prompted, re-run with `--yes`, or set "
                    "`auto_dismiss_dialogs = true` on the pi-coding agent type.",
                    source_path,
                )
                raise SystemExit(1)
            if not self._prompt_user_to_trust_workspace(source_path, trust_path):
                logger.error("User declined to trust {} in {}. Aborting agent creation.", source_path, trust_path)
                raise SystemExit(1)

        trust[source_key] = True
        trust_path.parent.mkdir(parents=True, exist_ok=True)
        with log_span("Persisting trusted pi source repo {} in {}", source_key, trust_path):
            trust_path.write_text(_serialize_pi_trust(trust))

    def _find_git_source_path(self, mngr_ctx: MngrContext) -> Path | None:
        """Source repo root for ``work_dir`` (the durable path persisted in global trust).

        For a worktree this is the parent repo, so a single trust grant covers
        every worktree of the same repo. A method (not a direct call to the free
        helper) so tests can override it without an active concurrency group.
        """
        return find_git_source_path(self.work_dir, mngr_ctx.concurrency_group)

    def _prompt_user_to_trust_workspace(self, source_path: Path, trust_path: Path) -> bool:
        """Ask the user to trust the agent's source directory in pi's global trust store.

        Returns True iff the user confirms. Refers to the *source* directory (the
        git repo root, or the bare work_dir if not a git repo) so the user sees a
        stable path across worktrees. Defaults to False so a stray Enter does not
        grant trust. A method (not a free function) so tests can override it.
        """
        logger.info(
            "\nSource directory {} is not yet trusted by pi.\n"
            "mngr needs to add a trust entry for it to {}\n"
            "so agents for this repo are not stranded at pi's trust dialog.\n",
            source_path,
            trust_path,
        )
        return click.confirm(f"Would you like to update {trust_path} to trust this directory?", default=False)

    def _seed_per_agent_workspace_trust(self, host: OnlineHostInterface) -> None:
        """Record the agent's *transient* work_dir as trusted in the per-agent pi trust store.

        This is the **transient** half of trust (see ``_ensure_source_repo_trusted``):
        pi matches trust on the exact canonical (realpath) cwd, and the agent runs
        in a per-agent worktree, so the entry that actually dismisses *this*
        agent's dialog is the worktree path. It goes only into the per-agent
        ``PI_CODING_AGENT_DIR/trust.json`` (which pi reads as ``<agent-dir>/trust.json``
        and which is deleted with the agent), never into the global store, so
        per-worktree paths don't accumulate there. The key is resolved on the host
        (``pwd -P``) to match how pi canonicalizes its cwd.
        """
        work_key = self._get_host_canonical_work_dir(host)
        trust_path = self.get_pi_config_dir() / _PI_TRUST_FILE_NAME
        try:
            existing = host.read_text_file(trust_path)
        except FileNotFoundError:
            existing = None
        trust = _read_pi_trust(existing, trust_path)
        if trust.get(work_key) is True:
            return
        trust[work_key] = True
        with log_span("Trusting pi workspace {} in {}", work_key, trust_path):
            host.write_text_file(trust_path, _serialize_pi_trust(trust))

    def _get_host_canonical_work_dir(self, host: OnlineHostInterface) -> str:
        """Resolve the work_dir to its canonical (realpath) form on the host.

        pi keys trust on ``realpathSync(cwd)``; matching that requires resolving
        symlinks on the host the agent runs on (``/tmp`` -> ``/private/tmp`` on
        macOS, etc.), so we ask the host rather than resolving locally. ``realpath``
        is a single binary (not the shell builtin ``cd``), so it runs under the
        test ``FakeHost`` too, and resolves symlinks the same way pi does.
        """
        result = host.execute_idempotent_command(f"realpath {shlex.quote(str(self.work_dir))}", timeout_seconds=5.0)
        if result.success and result.stdout.strip():
            return result.stdout.strip()
        logger.warning("Could not resolve canonical work dir for {}; using the literal path", self.work_dir)
        return str(self.work_dir)

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


@hookimpl
def register_agent_aliases() -> dict[str, str]:
    """Register ``pi`` as a short alias for the ``pi-coding`` agent type."""
    return {"pi": "pi-coding"}
