import importlib.resources
import json
import os
import shlex
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from loguru import logger
from pydantic import Field
from pydantic import field_validator

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import verify_pinned_cli_version
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import is_self_update_disabled
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.api.preservation import transfer_cloned_agent_session_store
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import WaitingReason
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

# The per-agent pi config dir, relative to the agent state dir (POSIX). Replaces
# ~/.pi/agent/ for this agent via ``PI_CODING_AGENT_DIR`` (see ``get_pi_config_dir``).
_PI_CONFIG_DIR_RELPATH: str = "plugin/pi_coding"

# pi's native resumable session store, relative to the agent state dir (POSIX):
# pi writes its session JSONLs under ``<PI_CODING_AGENT_DIR>/sessions``. Preserved
# on destroy so the conversation content survives (not just the dangling pointer).
# ``auth.json`` (a sibling under the config dir) is path-separate and excluded.
_PI_SESSIONS_DIR_RELPATH: str = f"{_PI_CONFIG_DIR_RELPATH}/sessions"

# How long to wait for the readiness sentinel at create time. Matches the
# TUI-ready budget in ``tui_utils`` -- startup can be slow on remote hosts that
# must render the TUI before the session loads.
_READY_TIMEOUT_SECONDS: float = 30.0

# Input delivery: mngr appends one JSON-encoded message per line to this file in
# the agent state dir; the lifecycle extension's watcher injects each new line
# into the live session via pi.sendUserMessage (no tmux keystrokes, TUI stays
# viewable). Kept in sync with INBOX_NAME in mngr_pi_lifecycle.ts.
_INBOX_FILE_NAME: str = "pi_inbox"

# The lifecycle marker the pi extension maintains while a turn is in flight
# (RUNNING vs WAITING). Kept in sync with ACTIVE_MARKER_NAME in mngr_pi_lifecycle.ts.
_ACTIVE_MARKER_NAME: str = "active"

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

# pi's native "auto-trust this run" flag (``--approve``/``-a``, "Trust project-local
# files for this run"). When passed, pi's ``resolveProjectTrusted`` short-circuits to
# trusted regardless of any ``.pi``/``.agents/skills`` trust inputs in the cwd and
# without a saved decision, so the interactive trust dialog never appears. mngr adds it
# to the launch command when ``auto_dismiss_dialogs`` is set, so an unattended agent is
# trusted via pi's own code path rather than relying solely on the seeded trust store.
_PI_APPROVE_FLAG: str = "--approve"


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


# The first line of a pi session JSONL is a ``{"type": "session", ..., "cwd": ...}``
# record; ``cwd`` is the absolute (realpath) directory pi resumes into. When that
# directory no longer exists pi refuses to resume ("Stored session working directory
# does not exist"), so adoption rewrites this field to the new agent's work_dir.
_PI_SESSION_RECORD_TYPE: str = "session"
_PI_SESSION_CWD_KEY: str = "cwd"


def _pi_session_store_dirs(mngr_ctx: MngrContext) -> list[Path]:
    """Return the pi ``sessions`` directories to search on the local host.

    Mirrors the claude resolver's scope: every live local mngr agent
    (``<host_dir>/agents/<id>/...``) and every preserved agent
    (``<host_dir>/preserved/<name>--<id>/...``), each of which stores its pi
    session JSONLs under ``plugin/pi_coding/sessions/<encoded-cwd>/``.

    Only the local host dir is scanned: an adopted session's files are copied
    onto the destination host from a local source path, so remote agents' stores
    are not searched here.
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return iter_agent_session_paths(local_host_dir, Path(_PI_SESSIONS_DIR_RELPATH))


def _resolve_adopt_session(adopt_session_arg: str, mngr_ctx: MngrContext, home_dir: Path | None = None) -> Path:
    """Resolve an adopt-session argument to the source pi session JSONL path.

    Accepts either:
    - An absolute path to a ``.jsonl`` session file.
    - A session id, searched across (all of):
      * the user-native store ``~/.pi/agent/sessions/`` (a plain ``pi`` run)
      * every live local mngr agent's ``plugin/pi_coding/sessions/``
      * every preserved (destroyed) agent's ``plugin/pi_coding/sessions/``

      The id matches a JSONL whose filename stem ends with the id (pi names files
      ``<timestamp>_<id>.jsonl``, so the bare id is the trailing component). All
      dirs are searched; a match in more than one is rejected as ambiguous (the
      user must pass the full ``.jsonl`` path), exactly as claude does.

    Returns the source session JSONL path.
    """
    if adopt_session_arg.endswith(".jsonl"):
        session_file = Path(adopt_session_arg).resolve()
        if not session_file.exists():
            raise UserInputError(f"pi session file not found: {session_file}")
        return session_file

    candidate_dirs = [_get_pi_home_dir(home_dir) / "sessions", *_pi_session_store_dirs(mngr_ctx)]
    search_dirs = dedupe_by_resolved_path(candidate_dirs)

    matches: list[Path] = []
    for sessions_dir in search_dirs:
        if sessions_dir.is_dir():
            for session_file in sessions_dir.glob(f"*/*{adopt_session_arg}.jsonl"):
                if session_file.stem == adopt_session_arg or session_file.stem.endswith(f"_{adopt_session_arg}"):
                    matches.append(session_file)

    # Don't enumerate the searched dirs in the not-found message: there is one per local mngr
    # agent, so the list can run long. The search scope is the user-native store, live agents,
    # and preserved agents.
    return require_unique_match(
        matches,
        not_found_message=(
            f"pi session {adopt_session_arg} not found. "
            "Check that the session id is correct, or pass an absolute path to the .jsonl file."
        ),
        ambiguous_message=(
            f"pi session {adopt_session_arg} found in multiple session stores; "
            "pass the absolute path to the .jsonl file to specify which one:"
        ),
    )


def _rewrite_pi_session_cwd(content: str, new_cwd: str) -> str:
    """Rewrite the embedded ``cwd`` in a pi session JSONL's first (``session``) record.

    pi binds a session to the cwd it was created in (the first JSONL line's
    ``cwd`` field) and refuses to resume when that directory no longer exists.
    After adopting a session into a new agent's (new) work_dir we rewrite this to
    the new work_dir so the resume never stalls at the missing-cwd dialog. Only
    the first record is touched; later records carry no ``cwd``. A first line that
    is not a ``session`` record is a schema we don't understand -- a hard error
    rather than a silent no-op (which would leave the dialog in place).
    """
    lines = content.splitlines()
    if not lines:
        raise UserInputError("pi session file is empty; cannot rebind its working directory")
    first = json.loads(lines[0])
    if not isinstance(first, dict) or first.get("type") != _PI_SESSION_RECORD_TYPE:
        raise UserInputError(
            f"pi session file's first record is not a {_PI_SESSION_RECORD_TYPE!r} record; "
            "cannot rebind its working directory"
        )
    first[_PI_SESSION_CWD_KEY] = new_cwd
    lines[0] = json.dumps(first)
    return "\n".join(lines) + "\n"


class PiAutoAllowRequiredError(PluginMngrError, ValueError):
    """Raised when pi's auto_allow_permissions is set to False, which pi cannot honor."""

    ...


class PiCodingAgentConfig(AgentTypeConfig):
    """Config for the pi-coding agent type."""

    command: CommandString = Field(
        default=CommandString("pi"),
        description="Command to run the pi coding agent.",
    )
    sync_home_settings: bool = Field(
        default=True,
        description="Share settings.json and resource dirs from ~/.pi/agent/ into the per-agent config dir.",
    )
    sync_auth: bool = Field(
        default=True,
        description="Share ~/.pi/agent/auth.json into the per-agent config dir.",
    )
    check_installation: bool = Field(
        default=True,
        description="Verify pi is installed (and install on remote hosts when allowed). If False, assumes it is already present.",
    )
    version: str | None = Field(
        default=None,
        description="Pin the pi CLI version to install (e.g., '1.2.3'). When set, installation runs "
        "`npm install -g @earendil-works/pi-coding-agent@<version>` and provisioning verifies the installed "
        "pi matches, erroring on a mismatch. When None (the default), installs the latest version.",
    )
    update_policy: AgentUpdatePolicy | None = Field(
        default=None,
        description="How to handle pi's startup version check. NEVER sets PI_SKIP_VERSION_CHECK=1 in the agent "
        "environment so pi does not phone home to compare against the latest release; AUTO leaves the check "
        "enabled. ASK has no interactive flow for pi and behaves like AUTO. When unset (the default), resolves "
        "to NEVER (check disabled) -- set AUTO to leave pi's startup version check enabled. (pi only notifies "
        "about updates -- it never self-replaces -- so this governs the startup check, not a background update.)",
    )
    auto_allow_permissions: bool = Field(
        default=True,
        description="pi runs every tool without an approval prompt, so it always operates unattended; "
        "setting this to False is an error because pi cannot enforce a deny.",
    )

    @field_validator("auto_allow_permissions")
    @classmethod
    def _require_auto_allow(cls, value: bool) -> bool:
        if not value:
            raise PiAutoAllowRequiredError(
                "pi runs every tool without an approval prompt, so it cannot honor "
                "auto_allow_permissions=False; pi always operates unattended."
            )
        return value

    resume_session: bool = Field(
        default=True,
        description=(
            "Resume this agent's pi session on start, so stop/start keeps context. Safe on first "
            "start (pi starts fresh when there is no recorded session yet)."
        ),
    )
    emit_common_transcript: bool = Field(
        default=True,
        description=(
            "Emit the transcript `mngr transcript` reads. The raw pi transcript is always "
            "captured; this gates only the common-envelope conversion."
        ),
    )
    emit_raw_transcript: bool = Field(
        default=True,
        description="Capture the raw pi message stream.",
    )
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description=(
            "Trust the workspace without prompting, suppressing pi's 'Trust project folder?' "
            "dialog. When set, mngr launches pi with `--approve` so pi auto-trusts the project "
            "folder for the run. Also implied by `mngr create --yes`. When False and the source "
            "repo is not already trusted, mngr prompts interactively and refuses to run "
            "non-interactively."
        ),
    )
    preserve_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
    )


# The npm package pi ships under (used for the auto-install command).
_PI_NPM_PACKAGE: str = "@earendil-works/pi-coding-agent"

# Env var that disables pi's startup version check (its phone-home to compare the
# installed version against the latest release). Set when the update policy is NEVER.
_PI_SKIP_VERSION_CHECK_ENV_VAR: str = "PI_SKIP_VERSION_CHECK"


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


class PiCodingAgent(
    BaseAgent[PiCodingAgentConfig],
    InteractiveAgentMixin,
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionAdoptionMixin,
    HasSessionPreservationMixin,
    HasUnattendedModeMixin,
    HasAutoInstallMixin,
):
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
        marker_path = self._get_agent_dir() / _ACTIVE_MARKER_NAME
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
        return self._get_agent_dir() / _PI_CONFIG_DIR_RELPATH

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Isolate pi's config per-agent and hand the lifecycle extension its knobs.

        ``MNGR_AGENT_STATE_DIR`` (where the marker, sentinel, and transcripts
        live) is injected by the host; the extension reads it directly. The
        remaining vars tell the extension which agent-type subdirectory to use
        and whether to emit each transcript layer.

        When the resolved update policy is NEVER, also sets PI_SKIP_VERSION_CHECK=1
        so pi does not run its startup version check. setdefault leaves an explicit
        user value alone.
        """
        env_vars["PI_CODING_AGENT_DIR"] = str(self.get_pi_config_dir())
        env_vars["MNGR_PI_AGENT_TYPE"] = _PI_AGENT_TYPE
        env_vars["MNGR_PI_EMIT_COMMON_TRANSCRIPT"] = "1" if self.agent_config.emit_common_transcript else "0"
        env_vars["MNGR_PI_EMIT_RAW_TRANSCRIPT"] = "1" if self.agent_config.emit_raw_transcript else "0"
        if is_self_update_disabled(self.agent_config.update_policy, is_unattended=not host.is_local):
            env_vars.setdefault(_PI_SKIP_VERSION_CHECK_ENV_VAR, "1")

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

        When ``auto_dismiss_dialogs`` is set, ``--approve`` is added so pi
        auto-trusts the project folder for the run (its native unattended
        path), and the interactive trust dialog never blocks the first message
        even when the cwd carries trust inputs (``.pi``/``.agents/skills``).

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
        if self.agent_config.auto_dismiss_dialogs:
            invocation = f"{invocation} {_PI_APPROVE_FLAG}"
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
            ensure_cli_installed(host, mngr_ctx, self.get_install_binary_name(), self.get_install_command())
            if config.version is not None:
                verify_pinned_cli_version(
                    host,
                    command=str(config.command),
                    binary_name=self.get_install_binary_name(),
                    pinned_version=config.version,
                )

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
        """Adopt an existing pi session (if requested) so the new agent resumes its conversation."""
        self.adopt_session(host, options, mngr_ctx)

    def adopt_session(self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        """Adopt one or more sessions so the new agent resumes existing context.

        Delegates the copy/resume ordering to ``adopt_sessions``: every ``--adopt``
        value is copied in (``_adopt_session``) and, additionally, a ``--from`` clone
        is copied in (``_adopt_cloned_session``); each call rebinds its session to
        this agent's work_dir and returns the resumable session-file path. The one
        actually resumed is the clone's when ``--from`` is given, otherwise the last
        ``--adopt`` value; ``_resume_adopted_session`` writes that as the
        ``pi_session_file`` pointer. With neither option set, the agent starts fresh.
        """
        adopt_sessions(
            options.adopt_session,
            options.source_agent_state_location,
            copy_explicit=lambda arg: self._adopt_session(host, arg),
            copy_clone=lambda location: self._adopt_cloned_session(host, location),
            resume=lambda session_file: self._resume_adopted_session(host, Path(session_file)),
        )

    def _adopt_session(self, host: OnlineHostInterface, adopt_arg: str) -> str:
        """Copy the resolved session into this agent's store, rebind it, and return its file path.

        Steps:
        1. Resolve ``adopt_arg`` (id or path) to a source JSONL on the local host.
        2. Copy the source's encoded-cwd subdir into this agent's ``sessions/`` so the
           store mirrors pi's own layout (the subdir name is cosmetic -- pi resumes by
           the absolute path recorded later). Additive: multiple ``--adopt`` values
           each land in their own subdir, so all are available in the new agent.
        3. Rebind the adopted JSONL to this agent's work_dir (so pi never stalls at
           its missing-cwd dialog) and return its absolute path; the resume pointer is
           written separately, only for the session actually resumed.
        """
        source_file = _resolve_adopt_session(adopt_arg, self.mngr_ctx)
        sessions_dir = self.get_pi_config_dir() / "sessions"
        dest_subdir = sessions_dir / source_file.parent.name
        with log_span("Adopting pi session {} into {}", source_file, dest_subdir):
            host.copy_directory(host, source_file.parent, dest_subdir)
        adopted_file = dest_subdir / source_file.name
        self._rebind_adopted_session(host, adopted_file)
        return str(adopted_file)

    def _adopt_cloned_session(self, host: OnlineHostInterface, source_location: HostLocation) -> str | None:
        """Transfer the source agent's pi session into a ``--from`` clone and return its file path.

        A generic ``--from`` clone copies the source *workspace* but not the source
        agent's *state dir*, so the cloned pi has no session to resume. This picks
        the most-recent session JSONL *on the source* (so the choice is unaffected
        by sessions an earlier ``--adopt`` may have already placed in the shared
        destination store), transfers the source's native session store into this
        agent's store, and rebinds the transferred copy of that session to this
        agent's work_dir.

        Warns and returns ``None`` if the source has no pi session store, or a store
        with no resumable session JSONL: a ``--from`` clone carries the source's
        workspace, and resuming its conversation is a bonus -- a source that never
        started a session simply starts fresh rather than failing the clone.
        """
        store_relpath = Path(_PI_SESSIONS_DIR_RELPATH)
        source_store = source_location.path / store_relpath
        source_host = source_location.host
        if not source_host.path_exists(source_store):
            logger.warning(
                "Clone adopt: no pi session store at source {}; starting the clone without a resumed session.",
                source_store,
            )
            return None
        # Choose on the source, where the store holds only the source agent's own
        # sessions -- the destination store may already contain --adopt sessions.
        latest_on_source = source_host.execute_idempotent_command(
            f"ls -t {shlex.quote(str(source_store))}/*/*.jsonl 2>/dev/null | head -n1",
            timeout_seconds=5.0,
        )
        if not (latest_on_source.success and latest_on_source.stdout.strip()):
            logger.warning(
                "Clone adopt: source pi session store {} has no session JSONL "
                "(ls success={}, stderr={!r}); starting the clone without a resumed session.",
                source_store,
                latest_on_source.success,
                latest_on_source.stderr.strip(),
            )
            return None
        latest_relative = Path(latest_on_source.stdout.strip()).relative_to(source_store)

        transfer_cloned_agent_session_store(host, self._get_agent_dir(), source_location, store_relpath)
        adopted_file = self._get_agent_dir() / store_relpath / latest_relative
        self._rebind_adopted_session(host, adopted_file)
        return str(adopted_file)

    def _rebind_adopted_session(self, host: OnlineHostInterface, adopted_file: Path) -> None:
        """Rebind an adopted session JSONL's embedded cwd to this agent's work_dir.

        pi binds a session to the cwd it was created in and refuses to resume when
        that directory no longer exists, so the cwd is rewritten to this agent's
        host-canonical work_dir. This does *not* write the resume pointer -- that is
        ``_resume_adopted_session``, called only for the single session resumed.
        Shared by ``--adopt`` and ``--from``.
        """
        new_cwd = self._get_host_canonical_work_dir(host)
        host.write_text_file(adopted_file, _rewrite_pi_session_cwd(host.read_text_file(adopted_file), new_cwd))
        logger.info("Adopted pi session {} (rebound cwd -> {})", adopted_file, new_cwd)

    def _resume_adopted_session(self, host: OnlineHostInterface, adopted_file: Path) -> None:
        """Point resume at an already-rebound adopted session.

        Writes the session's absolute path to ``pi_session_file`` so the launch
        ``pi --session <file>`` resumes it. Called once, for the single session
        chosen by ``adopt_sessions`` (the ``--from`` clone, else the last ``--adopt``).
        """
        host.write_text_file(self._get_agent_dir() / _SESSION_FILE_NAME, str(adopted_file))
        logger.info("Resuming adopted pi session {}", adopted_file)

    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        preserve_agent_state(_pi_coding_preserved_items(), self, host)

    def is_unattended_enabled(self) -> bool:
        # pi has no tool-approval gate, so it always runs unattended; the config
        # field is pinned True (False is rejected at validation).
        return self.agent_config.auto_allow_permissions

    def get_install_binary_name(self) -> str:
        return "pi"

    def get_install_command(self) -> str:
        version = self.agent_config.version
        package = f"{_PI_NPM_PACKAGE}@{version}" if version is not None else _PI_NPM_PACKAGE
        return f"npm install -g {shlex.quote(package)}"

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Preserve transcripts and the session-file pointer before the state dir is deleted.

        The per-agent config dir is deleted with the agent state, so there is no
        other cleanup to do.
        """
        if self.agent_config.preserve_on_destroy:
            self.preserve_session_state(host)


def _pi_coding_preserved_items() -> list[PreservedItem]:
    """Return the files to preserve from a pi-coding agent's state directory.

    The raw and common transcripts, the recorded session-file pointer (which
    records where pi stored the conversation, used to resume it), and pi's native
    resumable session store directory. Preserving the store keeps the conversation
    content itself, not just the (post-destroy dangling) pointer. ``auth.json`` is a
    path-separate sibling of the store and is excluded (and absent under env-var auth).
    """
    return [
        *build_transcript_preserved_items(_PI_AGENT_TYPE),
        PreservedItem(rel_path=_SESSION_FILE_NAME, kind=FileType.FILE),
        PreservedItem(rel_path=_PI_SESSIONS_DIR_RELPATH, kind=FileType.DIRECTORY),
    ]


def _pi_coding_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    """Return the items to preserve for a discovered (offline) pi-coding agent, or None to skip it."""
    return flag_gated_items(ref, "preserve_on_destroy", _pi_coding_preserved_items())


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting, or None while it is active.

    pi has no tool-approval gate, so it can never be blocked on a permission
    prompt: ``is_blocked_on_permission`` is always False and the only possible
    reason is ``END_OF_TURN`` (the agent is idle). Wired through the same shared
    ``classify_waiting_reason`` the other plugins use, so the single-value result
    is a real extension point if pi ever gains an approval gate.
    """
    agent_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    is_active = host.path_exists(agent_dir / _ACTIVE_MARKER_NAME)
    return classify_waiting_reason(is_active, is_blocked_on_permission=False)


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose pi-coding-specific agent fields for listing."""
    return (_PI_AGENT_TYPE, {"waiting_reason": _waiting_reason})


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve pi-coding transcripts from the host's volume before it is destroyed.

    Mirrors ``PiCodingAgent.on_destroy`` for the offline path, where a host is
    destroyed without per-agent ``on_destroy`` calls but agent state still lives
    on the host's persisted volume.
    """
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName(_PI_AGENT_TYPE), _pi_coding_items_to_preserve_for_discovered_agent
    )


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Fail-fast pre-resolution of pi ``--adopt`` session ids (see ``run_adopt_session_preflight``)."""
    run_adopt_session_preflight(
        args.agent_options.agent_type,
        args.agent_options.adopt_session,
        mngr_ctx,
        PiCodingAgent,
        lambda session_arg: _resolve_adopt_session(session_arg, mngr_ctx),
    )
    return None


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the pi-coding agent type."""
    return ("pi-coding", PiCodingAgent, PiCodingAgentConfig)


@hookimpl
def register_agent_aliases() -> dict[str, str]:
    """Register ``pi`` as a short alias for the ``pi-coding`` agent type."""
    return {"pi": "pi-coding"}
