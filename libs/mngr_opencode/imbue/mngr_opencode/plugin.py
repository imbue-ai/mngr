"""``mngr_opencode`` plugin -- registers the ``opencode`` agent type for OpenCode.

OpenCode (https://opencode.ai) is an open-source terminal AI coding agent. It is
architecturally unlike Claude Code / Antigravity: a client-server app whose TUI
embeds a server, with sessions/messages persisted in a SQLite db, and **no
POSIX-sh hook mechanism**. Its blessed extension point is an in-process
TypeScript plugin. mngr leans on that and on OpenCode's config-dir env vars
(the *preferred* isolation shape -- no ``$HOME`` relocation).

Per-agent isolation (``provision`` / ``assemble_command``)
----------------------------------------------------------
Two env vars, injected only on the OpenCode process via an ``env`` prefix:

* ``OPENCODE_CONFIG_DIR`` -> a per-agent config dir holding ``opencode.json``
  (model + permission policy) and ``plugin/`` (the lifecycle plugin, auto-loaded).
* ``XDG_DATA_HOME`` -> a per-agent data root, so OpenCode's
  ``opencode/{opencode.db,auth.json,storage,log}`` -- and therefore sessions
  (resume) and credentials -- are per-agent.

Auth: the per-agent ``auth.json`` is a symlink to the user's shared
``~/.local/share/opencode/auth.json`` (OpenCode writes it in place, so a login
in any agent writes through and authenticates the rest -- the
``mngr_antigravity`` oauth-token mechanism, applied to OpenCode's auth file).
Copy mode (``symlink_auth=False``) gives full isolation without sharing.

Lifecycle marker (RUNNING vs WAITING): the in-process plugin
(``resources/mngr_opencode_plugin.ts``) touches ``$MNGR_AGENT_STATE_DIR/active``
when a session goes busy and removes it when the **root** session goes idle.
OpenCode reports status per session and the root session stays busy for the
whole turn -- including while task-tool subagents (child sessions) run -- so
gating the clear on the root session id keeps the agent RUNNING until the entire
turn is done. This is simpler than Antigravity's ``fullyIdle`` matching because
OpenCode's per-session status already encodes "is the root done".

Resume: the plugin records the root session id; ``assemble_command`` appends
``--continue`` once the agent has a session, so stop/start keeps context (the
session store is the per-agent SQLite db, which survives the hard kill).

Transcript: the plugin writes the raw transcript in-process (no SQLite-reading
shell script, so no ``sqlite3`` dependency on remote hosts); a backgrounded
converter (``resources/opencode_common_transcript.sh``) turns it into the common
format ``mngr transcript`` reads.

Trust / onboarding: OpenCode has no "trust this folder?" dialog or blocking
first-run NUX (verified live), so -- unlike Claude/Antigravity -- there is
nothing to seed or gate before the first message.
"""

from __future__ import annotations

import importlib.resources
import re
import shlex
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
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.common import copy_on_host
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_opencode import resources as _opencode_resources
from imbue.mngr_opencode.opencode_config import PLUGIN_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
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

# OpenCode env vars that isolate config and data per agent (injected only on the
# OpenCode process). ``OPENCODE_CONFIG_DIR`` points at the dir holding
# opencode.json + plugin/; ``XDG_DATA_HOME`` is the root under which OpenCode
# keeps ``opencode/`` (db, auth, storage, logs).
_OPENCODE_CONFIG_DIR_ENV_VAR: Final[str] = "OPENCODE_CONFIG_DIR"
_XDG_DATA_HOME_ENV_VAR: Final[str] = "XDG_DATA_HOME"

# Top-level flag that resumes the most recent (root) session in the data dir.
# OpenCode's ``--continue`` already filters to sessions with no parent, so it
# never resumes a subagent's session.
_CONTINUE_FLAG: Final[str] = "--continue"

# Length of the message tail compared against the pane (mirrors the probe length
# in ``tui_utils._check_paste_content``, which is private and cannot be imported).
_PASTE_PROBE_LENGTH: Final[int] = 60
_TMUX_PASTE_INDICATOR: Final[str] = "[Pasted text "
_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")


def _load_opencode_resource(filename: str) -> str:
    """Load a resource file from the mngr_opencode resources package."""
    resource_files = importlib.resources.files(_opencode_resources)
    return resource_files.joinpath(filename).read_text()


def _is_paste_echoed(agent: BaseAgent[Any], message: str) -> bool:
    """Return whether ``message`` appears to have landed in the pane's input.

    Mirrors ``tui_utils._check_paste_content`` (private, so not importable): a
    tmux bracketed-paste indicator counts as success, otherwise a normalized
    tail of the message must be present in the normalized pane text (robust to
    input-box line wrapping).
    """
    content = agent._capture_pane_content(agent.tmux_target)
    if content is None:
        return False
    if _TMUX_PASTE_INDICATOR in content:
        return True
    normalized_message = _NON_ALNUM_RE.sub("", message.lower())
    if not normalized_message:
        return True
    probe = normalized_message[-_PASTE_PROBE_LENGTH:]
    return probe in _NON_ALNUM_RE.sub("", content.lower())


class OpenCodeAgentConfig(AgentTypeConfig):
    """Config for the opencode agent type."""

    command: CommandString = Field(
        default=CommandString("opencode"),
        description="Command to run the opencode agent.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the opencode agent.",
    )
    # config_overrides mirrors mngr_antigravity's settings_overrides: a free-form
    # blob merged last into the per-agent opencode.json. Covers ``model``
    # ("provider/model"), the ``permission`` policy block ({"bash": {"git *":
    # "allow", "rm -rf *": "deny"}, "edit": "ask", ...}), ``small_model``, etc.
    # Combined with auto_allow_permissions, the wildcard allow that flag injects
    # is applied first and a config_overrides ``permission`` block (if any) wins.
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged last into the per-agent opencode.json. "
        'Common keys: model ("provider/model"), permission ({"bash": {...}, "edit": "ask"}). '
        'Example: {"model": "anthropic/claude-sonnet-4-5", "permission": {"bash": {"rm -rf *": "deny"}}}.',
    )
    # sync_global_config mirrors mngr_antigravity's sync_home_settings: when True
    # (default), the per-agent opencode.json starts from a copy of the user's real
    # ~/.config/opencode/opencode.json (so the agent inherits the user's model /
    # provider / theme defaults and is usable out of the box); config_overrides
    # layer on top. When False, the base is an empty config.
    sync_global_config: bool = Field(
        default=True,
        description="Whether to base the per-agent opencode.json on a copy of the user's real "
        "~/.config/opencode/opencode.json (True, default) or start from an empty base (False).",
    )
    # symlink_auth mirrors mngr_antigravity's symlink_oauth_token. With the
    # default (symlink), the per-agent auth.json is a symlink to the shared
    # ~/.local/share/opencode/auth.json -- created even when that shared file
    # doesn't exist yet. OpenCode writes auth.json in place, so the first agent's
    # login writes through to the shared path and authenticates every other agent
    # (and propagates refreshes). Copy mode (False) gives full isolation (no
    # sharing) and only seeds if the shared file already exists.
    symlink_auth: bool = Field(
        default=True,
        description="Symlink (True, default) each per-agent auth.json to the shared "
        "~/.local/share/opencode/auth.json, so one agent's login authenticates all agents. "
        "Copy (False) for full isolation (no sharing).",
    )
    # auto_allow_permissions injects a wildcard ``permission`` allow into the
    # per-agent opencode.json (auto-approve every action not explicitly denied) --
    # the config analog of OpenCode's ``run --dangerously-skip-permissions``. The
    # TUI honors the config ``permission`` block, so no CLI flag is needed.
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


class OpenCodeAgent(InteractiveTuiAgent[OpenCodeAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for OpenCode."""

    # Stable footer-hint substring OpenCode renders only once the input prompt is
    # drawn and ready. Deliberately not the ASCII-art splash banner, which renders
    # before the input row exists (mngr would paste into the void). Verified
    # against the live TUI.
    TUI_READY_INDICATOR: ClassVar[str] = "ctrl+p commands"

    # Paste self-healing. OpenCode is a client-server TUI: the input footer first
    # paints within ~2-3s, but the embedded server then finishes initializing and
    # the client *repaints* (the screen briefly clears). Keystrokes sent during
    # that repaint window are silently dropped, and nothing in the create/message
    # flow waits for the repaint before the first ``send_message`` (a running
    # agent's send path has no readiness gate). So the paste self-heals: send,
    # confirm the text echoed, and on a drop clear the input and re-send. Once the
    # agent is past startup the first attempt lands, so a stable agent pays no
    # penalty. (The OpenCode form of the spec's dimension-E "input not live yet"
    # gotcha.) ClassVars so a test subclass can shrink them.
    _MAX_PASTE_ATTEMPTS: ClassVar[int] = 5
    _PASTE_ECHO_TIMEOUT_SECONDS: ClassVar[float] = 3.0
    _PASTE_ECHO_POLL_INTERVAL_SECONDS: ClassVar[float] = 0.3
    # tmux key that clears OpenCode's input line (readline-style kill-line) so a
    # re-send can never append to a partially-landed earlier attempt.
    _CLEAR_INPUT_KEY: ClassVar[str] = "C-u"

    def get_expected_process_name(self) -> str:
        # OpenCode ships as a single bun-compiled binary; ps/tmux report ``opencode``.
        return "opencode"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # OpenCode's TUI has no UserPromptSubmit-style hook to key a tmux wait-for
        # off, and no input placeholder that clears on submit to poll, so -- as
        # with Antigravity -- a best-effort Enter after ``wait_for_paste_visible``
        # (which already confirmed the text landed) is the right strategy.
        send_enter_best_effort(self, tmux_target)

    def send_message(self, message: str) -> None:
        """Send a message, re-sending if OpenCode drops the paste during its post-launch repaint.

        Mirrors the base ``InteractiveTuiAgent.send_message`` (lock, preflight,
        paste, submit) but replaces the single paste + visibility-poll with a
        clear-and-retry loop, because OpenCode silently ignores keystrokes for a
        moment after the TUI first paints and the send path has no readiness gate
        to wait that out (see ``_MAX_PASTE_ATTEMPTS``).
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)
            self._paste_message_with_retry(message)
            self._send_enter_and_validate(self.tmux_target)

    def _paste_message_with_retry(self, message: str) -> None:
        """Paste ``message`` into the TUI, re-sending until it echoes or attempts are exhausted.

        Clears the input line before every retry so a late-landing earlier
        attempt cannot double the text. A stable agent lands on the first
        attempt and never clears or retries.
        """
        for attempt in range(self._MAX_PASTE_ATTEMPTS):
            if attempt > 0:
                self._clear_input_line()
            self._send_tmux_literal_keys(self.tmux_target, message)
            if poll_until(
                lambda: _is_paste_echoed(self, message),
                timeout=self._PASTE_ECHO_TIMEOUT_SECONDS,
                poll_interval=self._PASTE_ECHO_POLL_INTERVAL_SECONDS,
            ):
                return
        raise SendMessageError(
            str(self.name),
            f"OpenCode did not accept the pasted message after {self._MAX_PASTE_ATTEMPTS} attempts",
        )

    def _clear_input_line(self) -> None:
        """Clear OpenCode's input line via tmux (kill-line) before a paste retry."""
        result = self.host.execute_stateful_command(
            f"tmux send-keys -t {self.tmux_target.as_shell_arg()} {self._CLEAR_INPUT_KEY}"
        )
        if not result.success:
            raise SendMessageError(
                str(self.name), f"Failed to clear OpenCode input line: {result.stderr or result.stdout}"
            )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return no commands/ scripts: the raw transcript is written in-process.

        OpenCode has no native JSONL session file to tail and reading its SQLite
        db from a shell would need ``sqlite3`` (not guaranteed on remote hosts),
        so the in-process plugin (``mngr_opencode_plugin.ts``, provisioned into
        the config dir, not commands/) appends each message/part event to
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
        """Per-agent file recording the root OpenCode session id (written by the plugin).

        Its presence is the "this agent already has a session" signal
        ``assemble_command`` uses to decide whether to pass ``--continue``. Lives
        directly under the agent state dir so the plugin's
        ``$MNGR_AGENT_STATE_DIR/{ROOT_SESSION_FILENAME}`` and this path resolve to
        the same file.
        """
        return self._get_agent_dir() / ROOT_SESSION_FILENAME

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
        """Provision the per-agent config dir, lifecycle plugin, auth, and transcript scripts.

        Steps:

        1. Resolve the host user's real ``$HOME`` (shared-auth / global-config source).
        2. Write the per-agent ``opencode.json`` (a copy of the user's global config
           when ``sync_global_config``, plus the auto-allow permission block and
           ``config_overrides``) and the lifecycle plugin into the config dir.
        3. Point the per-agent ``auth.json`` at the shared host auth (symlink or copy).
        4. Install the common-transcript converter and the background supervisor
           under ``$MNGR_AGENT_STATE_DIR/commands/``.
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
                {_BACKGROUND_TASKS_SCRIPT_NAME: _load_opencode_resource(_BACKGROUND_TASKS_SCRIPT_NAME)},
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
        agent (and propagating refreshes). Copy mode copies the shared file in
        only if it exists, else leaves the agent to run OpenCode's login flow.
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
        """Build the full launch command.

        Composition (left to right):

        1. ``( bash opencode_background_tasks.sh <session> ) &`` -- backgrounded
           transcript-converter supervisor (only when ``emit_common_transcript``).
        2. ``{ <resume-prelude>; env OPENCODE_CONFIG_DIR=<cfg> XDG_DATA_HOME=<data>
           opencode <user_args> "$@"; }`` -- the foreground OpenCode process. The
           two env vars isolate config + data per agent and are injected only here
           (the supervisor subshell and tmux keep the real environment). OpenCode
           auto-loads the lifecycle plugin from ``<cfg>/plugin/`` and the model /
           permission policy from ``<cfg>/opencode.json``; no permission CLI flag
           is needed (auto-allow goes through the config).

        The resume-prelude appends ``--continue`` (resume the most recent root
        session) once the agent has run before -- detected by the presence of the
        plugin-written root-session file. It is shell-evaluated here because the
        stored command is replayed on every ``mngr start`` and the file may not
        exist yet on the first launch (passing ``--continue`` with no session
        would be wrong). ``--continue`` filters to sessions with no parent, so it
        never resumes a subagent's session; OpenCode's per-agent SQLite session
        store survives the hard kill ``mngr stop`` performs.

        Bash precedence note: ``A & B`` parses as ``A &`` then ``B``; the
        supervisor subshell is scoped to ``&`` and the OpenCode group is foreground.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)

        config_dir = self._get_opencode_config_dir()
        data_home = self._get_opencode_data_home()
        env_prefix = (
            f"env {_OPENCODE_CONFIG_DIR_ENV_VAR}={shlex.quote(str(config_dir))} "
            f"{_XDG_DATA_HOME_ENV_VAR}={shlex.quote(str(data_home))}"
        )

        # Resume the most recent root session via `--continue`, shell-evaluated
        # because the stored command is replayed on each restart and the
        # root-session file only exists once the agent has run. `set --` / "$@"
        # appends the flag without unquoted word-splitting under bash and zsh.
        quoted_root_file = shlex.quote(str(self._get_root_session_file_path()))
        resume_prelude = f"set --; if [ -s {quoted_root_file} ]; then set -- {_CONTINUE_FLAG}; fi"

        opencode_group = f'{{ {resume_prelude}; {env_prefix} {base_command} "$@" ; }}'
        if not self.is_common_transcript_enabled:
            return CommandString(opencode_group)
        background_cmd = self._build_background_tasks_command()
        return CommandString(f"{background_cmd} {opencode_group}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the opencode agent type."""
    return ("opencode", OpenCodeAgent, OpenCodeAgentConfig)
