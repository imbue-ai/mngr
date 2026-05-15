from __future__ import annotations

import importlib.resources
import json
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final

from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_and_poll_for_cleared_indicator
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_gemini import resources as _gemini_resources
from imbue.mngr_gemini.gemini_config import build_permission_auto_allow_hooks_config
from imbue.mngr_gemini.gemini_config import build_readiness_hooks_config
from imbue.mngr_gemini.gemini_config import get_user_gemini_settings_path
from imbue.mngr_gemini.gemini_config import merge_hooks_config
from imbue.mngr_gemini.gemini_config import read_gemini_settings
from imbue.mngr_gemini.gemini_config import serialize_gemini_settings

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"

# Name of the readiness sentinel file the SessionStart hook touches. Polled by
# ``GeminiAgent.wait_for_ready_signal`` to detect that the Gemini session has
# fully started. Kept in sync with the path embedded in
# ``build_readiness_hooks_config`` (``$MNGR_AGENT_STATE_DIR/session_started``).
_READINESS_SENTINEL_FILENAME: Final[str] = "session_started"

# Matches mngr_claude's _READY_SIGNAL_TIMEOUT_SECONDS. Governs only the
# sentinel-file poll in ``wait_for_ready_signal`` below. The TUI banner poll
# (run by ``InteractiveTuiAgent.wait_for_ready_signal`` when
# ``is_creating=True``) has its own independent budget --
# ``_TUI_READY_TIMEOUT_SECONDS`` in ``mngr.agents.tui_utils`` -- and ignores
# the ``timeout`` argument we forward through ``super()``. Worst-case total
# wait is therefore roughly ``start_action duration +
# (is_creating ? _TUI_READY_TIMEOUT_SECONDS : 0) + _READY_SIGNAL_TIMEOUT_SECONDS``.
_READY_SIGNAL_TIMEOUT_SECONDS: Final[float] = 10.0

# Plugin-scoped subdir inside the per-agent state dir. Mirrors how
# ``mngr_claude`` namespaces its files under ``plugin/claude/anthropic/``
# inside ``$MNGR_AGENT_STATE_DIR``. Gemini reads ``.gemini/`` (note the
# leading dot) underneath the home dir, so the actual settings dir Gemini
# touches is ``$MNGR_AGENT_STATE_DIR/plugin/gemini/.gemini/``.
_PLUGIN_STATE_SUBDIR: Final[tuple[str, ...]] = ("plugin", "gemini")

# Subdir Gemini CLI expects inside ``GEMINI_CLI_HOME``. See
# ``packages/core/src/utils/paths.ts`` in google-gemini/gemini-cli:
# ``homedir()`` is overridable via the env var, but every settings/auth path
# still appends ``.gemini/``. Documented in
# https://github.com/google-gemini/gemini-cli/issues/23622.
_GEMINI_HOME_SUBDIR: Final[str] = ".gemini"

# Set in the agent's environment so Gemini reads a per-agent home dir for
# all of ``.gemini/`` (settings, trusted folders, credentials, history, tmp,
# installation id) instead of the user's ``~/.gemini/``. Mirrors
# ``mngr_claude``'s use of ``CLAUDE_CONFIG_DIR`` for total per-agent config
# isolation. Documented as the user-isolation primitive at
# https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/enterprise.md
# (under "User isolation in shared environments").
_GEMINI_CLI_HOME_ENV_VAR: Final[str] = "GEMINI_CLI_HOME"

# Files the user authenticated against ``~/.gemini/`` produces that need to
# be visible to the per-agent home for the agent to inherit the user's
# Google login. ``oauth_creds.json`` carries the refresh/access tokens (when
# Gemini hasn't migrated them to the OS keychain on this version);
# ``google_accounts.json`` is the account selector the keychain lookup uses
# when tokens have been migrated; ``installation_id`` is the per-installation
# UUID Gemini sends to its backend. Symlinked in from ``~/.gemini/`` rather
# than copied so the agent picks up re-auth state changes.
_AUTH_ARTIFACT_FILENAMES: Final[tuple[str, ...]] = (
    "oauth_creds.json",
    "google_accounts.json",
    "installation_id",
)


def _load_gemini_resource_script(filename: str) -> str:
    """Load a resource script from the mngr_gemini resources package."""
    resource_files = importlib.resources.files(_gemini_resources)
    script_path = resource_files.joinpath(filename)
    return script_path.read_text()


class GeminiAgentConfig(AgentTypeConfig):
    """Config for the gemini agent type."""

    command: CommandString = Field(
        default=CommandString("gemini"),
        description="Command to run gemini agent",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the gemini agent.",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript at "
        "events/gemini/common_transcript/events.jsonl. When enabled, a background "
        "process polls gemini's session JSONL files and converts user, assistant, "
        "tool-call, and tool-result events into the common schema that "
        "`mngr transcript` reads.",
    )
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, install a BeforeTool hook (wildcard matcher) that "
        'auto-approves every tool call by emitting `{"decision":"allow"}` on '
        "stdout. Gemini analogue of mngr_claude's `auto_allow_permissions` "
        "flag (which wires Claude Code's `PermissionRequest` hook); Gemini "
        "has no `PermissionRequest` event, so `BeforeTool` is the equivalent "
        "extension point. Prefer this over the `-y`/`--approval-mode yolo` "
        "CLI flag: the hook survives admin policies that disable yolo mode "
        "(`security.disableYoloMode`) and shows up explicitly in Gemini's "
        "`--debug` hook-registry output.",
    )


class GeminiAgent(InteractiveTuiAgent[GeminiAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for Google's Gemini CLI."""

    # Stable banner string in gemini's header that persists for the lifetime
    # of the session; polled at startup to confirm the TUI is rendered.
    TUI_READY_INDICATOR = "Gemini CLI"

    # Dynamic placeholder in gemini's input row: shown when the input is
    # empty, hidden the moment text occupies the input, and reappears once
    # Enter is consumed and the input clears. The poll-and-retry strategy
    # below uses this to detect successful submission and retry on swallowed
    # keystrokes.
    INPUT_CLEARED_INDICATOR: ClassVar[str] = "Type your message"

    def get_expected_process_name(self) -> str:
        # `gemini` is a `#!/usr/bin/env node` script and (unlike `claude`) does
        # not override `process.title`, so the running process shows up as
        # `node` in ps/tmux. Report that so lifecycle detection finds it.
        return "node"

    def _send_enter_and_validate(self, tmux_target: str) -> None:
        # Gemini has no UserPromptSubmit-style hook, so confirm submission by
        # polling for the input-row placeholder to reappear once Enter clears
        # the typed text.
        send_enter_and_poll_for_cleared_indicator(
            self,
            tmux_target,
            cleared_indicator=self.INPUT_CLEARED_INDICATOR,
        )

    def _get_readiness_sentinel_path(self) -> Path:
        """Path the ``SessionStart`` hook touches once Gemini has finished starting up."""
        return self._get_agent_dir() / _READINESS_SENTINEL_FILENAME

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Run start_action, then wait for both the TUI banner and the readiness sentinel.

        Polls for the ``$MNGR_AGENT_STATE_DIR/session_started`` file that the
        ``SessionStart`` hook installed by ``provision`` touches when the
        Gemini session is ready. Mirrors ``ClaudeAgent.wait_for_ready_signal``;
        the super-call still polls the TUI banner (``InteractiveTuiAgent``'s
        contract), so this method adds the sentinel poll on top.
        """
        if timeout is None:
            timeout = _READY_SIGNAL_TIMEOUT_SECONDS

        sentinel_path = self._get_readiness_sentinel_path()
        with log_span("Waiting for session_started file (timeout={}s)", timeout):
            with log_span("Calling start_action..."):
                super().wait_for_ready_signal(is_creating, start_action, timeout)
            if poll_until(
                lambda: self._check_file_exists(sentinel_path),
                timeout=timeout,
                poll_interval=0.05,
            ):
                return
            raise AgentStartError(
                str(self.name),
                f"Agent did not signal readiness within {timeout}s. "
                "This may indicate Gemini CLI failed to start or that the per-agent "
                f"home dir was not wired in ({_GEMINI_CLI_HOME_ENV_VAR}).",
            )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the gemini transcript converter script."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_gemini_resource_script(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def _get_gemini_cli_home(self) -> Path:
        """Per-agent value for ``GEMINI_CLI_HOME``.

        Gemini reads ``<this dir>/.gemini/`` for settings, trusted folders,
        credentials, history, tmp, and installation id. Living at
        ``$MNGR_AGENT_STATE_DIR/plugin/gemini`` keeps every agent fully
        isolated and never touches the user's ``~/.gemini/``.
        """
        return self._get_agent_dir().joinpath(*_PLUGIN_STATE_SUBDIR)

    def _get_relocated_gemini_dir(self) -> Path:
        """``<GEMINI_CLI_HOME>/.gemini/`` -- the actual config dir Gemini touches."""
        return self._get_gemini_cli_home() / _GEMINI_HOME_SUBDIR

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Point Gemini at the per-agent home dir via ``GEMINI_CLI_HOME``.

        Mirrors ``mngr_claude``'s use of ``CLAUDE_CONFIG_DIR``: every
        ``.gemini/`` artifact (settings, trusted folders, credentials,
        history, tmp, installation id) now resolves under the per-agent
        state dir instead of the user's ``~/.gemini/``. Provisioning seeds
        this dir with merged settings, workspace trust, and symlinks back to
        the user's auth artifacts so the agent inherits the user's login
        without exposing any of mngr's hooks to the user's workspace.
        """
        env_vars[_GEMINI_CLI_HOME_ENV_VAR] = str(self._get_gemini_cli_home())

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Seed the per-agent ``GEMINI_CLI_HOME`` dir and (optionally) the transcript watcher.

        Three things land in ``$MNGR_AGENT_STATE_DIR/plugin/gemini/.gemini/``:

        1. ``settings.json`` -- the user's ``~/.gemini/settings.json``
           contents (so ``security.auth.selectedType`` is preserved and the
           agent knows which auth method to use) with mngr's hooks merged
           in via ``merge_hooks_config``.
        2. ``trustedFolders.json`` -- explicit trust for ``work_dir`` so the
           "Do you trust this folder?" gate doesn't consume a tmux keystroke
           at startup.
        3. Symlinks back to the user's ``oauth_creds.json`` /
           ``google_accounts.json`` / ``installation_id`` so the agent
           inherits the user's existing Google login. Symlinks rather than
           copies so re-auth in the user's dir flows through.

        The transcript-watcher install delegates the enable-flag check and
        the upload to :func:`maybe_provision_common_transcript_scripts`;
        when ``agent_config.emit_common_transcript`` is ``False`` nothing is
        written and ``assemble_command`` will not prepend the watcher.
        """
        self._install_settings(host)
        self._install_workspace_trust(host)
        self._symlink_user_auth_artifacts(host)
        with mngr_ctx.concurrency_group.make_concurrency_group("gemini_provisioning") as concurrency_group:
            maybe_provision_common_transcript_scripts(
                self,
                host,
                self._get_agent_dir(),
                concurrency_group,
            )

    def _install_settings(self, host: OnlineHostInterface) -> None:
        """Merge mngr's hooks into the user's settings.json and write to the per-agent dir.

        Starts from the user's ``~/.gemini/settings.json`` contents so the
        agent inherits ``security.auth.selectedType``, custom MCP servers,
        approval mode, GEMINI.md filename overrides, etc. Layers mngr's
        configured hook builders on top using ``merge_hooks_config`` so any
        user-managed hooks under shared event keys are preserved rather than
        clobbered.
        """
        settings = read_gemini_settings(get_user_gemini_settings_path())

        builders: list[dict[str, Any]] = [build_readiness_hooks_config()]
        if self.agent_config.auto_allow_permissions:
            builders.append(build_permission_auto_allow_hooks_config())
        for builder_output in builders:
            merged = merge_hooks_config(settings, builder_output)
            # Each builder adds a matcher group that isn't in the user's
            # settings (mngr commands embed ``$MNGR_AGENT_STATE_DIR`` in the
            # command body, which a user-managed hook would not), so
            # ``merge_hooks_config`` always finds something to append.
            assert merged is not None
            settings = merged

        host.write_text_file(self._get_relocated_gemini_dir() / "settings.json", serialize_gemini_settings(settings))

    def _install_workspace_trust(self, host: OnlineHostInterface) -> None:
        """Mark ``work_dir`` as trusted in the per-agent ``trustedFolders.json``."""
        trusted = {str(self.work_dir.resolve()): "TRUST_FOLDER"}
        host.write_text_file(
            self._get_relocated_gemini_dir() / "trustedFolders.json",
            json.dumps(trusted, indent=2) + "\n",
        )

    def _symlink_user_auth_artifacts(self, host: OnlineHostInterface) -> None:
        """Symlink the user's Google-account auth files into the per-agent dir.

        Local-only for now. Remote hosts would need a copy-and-keep-in-sync
        strategy (rsync, or a dedicated host-aware credential-sync hook --
        mirrors what ``mngr_claude._provision_local_credentials`` does for
        ``.credentials.json``). Raises ``NotImplementedError`` on remote
        hosts so the gap is obvious rather than failing silently with a
        confusing auth error later.
        """
        if not host.is_local:
            raise NotImplementedError(
                "mngr_gemini does not yet support remote hosts: the user's Google login "
                "artifacts need to be replicated under the per-agent GEMINI_CLI_HOME, and "
                "the local-symlink strategy does not extend across machines."
            )

        relocated_dir = self._get_relocated_gemini_dir()
        relocated_dir.mkdir(parents=True, exist_ok=True)
        user_dir = get_user_gemini_settings_path().parent
        for name in _AUTH_ARTIFACT_FILENAMES:
            source = user_dir / name
            if not source.exists():
                continue
            link = relocated_dir / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(source)

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the gemini command with stale-sentinel cleanup and the transcript watcher.

        Inserts ``rm -f $MNGR_AGENT_STATE_DIR/session_started`` as a
        foreground step that runs to completion before ``gemini`` launches,
        so that a leftover sentinel from a previous run cannot make
        ``wait_for_ready_signal`` succeed before the new Gemini session has
        actually started (relevant on every restart, not just first launch).

        When ``is_common_transcript_enabled`` is True, also launches the
        transcript watcher fire-and-forget as a backgrounded subshell
        (``( bash ... ) &``) placed *before* the ``rm`` step. Placement
        matters: ``A && B & C`` in bash parses as ``( A && B ) &`` followed
        by ``C``, so writing ``rm -f X && ( watcher ) & gemini`` would push
        the ``rm`` into the background where it races gemini's startup.
        Putting the watcher first -- ``( watcher ) & rm -f X && gemini`` --
        confines ``&`` to the watcher subshell and leaves the rm in the
        foreground chain that precedes the agent invocation, matching
        ``mngr_claude``'s assembled-command shape.

        Bash does not propagate SIGHUP to background children of
        non-interactive shells by default, so the watcher may outlive the
        tmux session and continue polling until killed by host teardown or
        until its session inputs disappear.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        clear_sentinel = f"rm -f $MNGR_AGENT_STATE_DIR/{_READINESS_SENTINEL_FILENAME}"
        if not self.is_common_transcript_enabled:
            return CommandString(f"{clear_sentinel} && {base_command}")
        background_cmd = f"( bash $MNGR_AGENT_STATE_DIR/commands/{_COMMON_TRANSCRIPT_SCRIPT_NAME} ) &"
        return CommandString(f"{background_cmd} {clear_sentinel} && {base_command}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
