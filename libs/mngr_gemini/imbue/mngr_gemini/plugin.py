from __future__ import annotations

import importlib.resources
import shlex
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
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
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
from imbue.mngr_gemini.gemini_config import merge_hooks_config
from imbue.mngr_gemini.gemini_config import serialize_gemini_settings

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"

# Supervisor script provisioned to the agent's commands/ dir. Launched once
# as a backgrounded subshell from ``assemble_command``; it owns both
# watchers (the raw streamer and, when emit is enabled, the common
# converter) and restarts them on death. Mirrors mngr_claude's
# claude_background_tasks.sh pattern but without an activity-tracker (gemini
# has no UserPromptSubmit-style hook that would write an ``active`` file).
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "gemini_background_tasks.sh"

# Name of the readiness sentinel file the SessionStart hook touches. Polled by
# ``GeminiAgent.wait_for_ready_signal`` to detect that the Gemini session has
# fully started. Kept in sync with the path embedded in
# ``build_readiness_hooks_config`` (``$MNGR_AGENT_STATE_DIR/session_started``).
_READINESS_SENTINEL_FILENAME: Final[str] = "session_started"

# Matches mngr_claude's _READY_SIGNAL_TIMEOUT_SECONDS. Gemini start-up is
# generally faster than Claude's because we don't have plugin/credential
# provisioning to wait on. Governs only the sentinel-file poll in
# ``wait_for_ready_signal`` below. The TUI banner poll (run by
# ``InteractiveTuiAgent.wait_for_ready_signal`` when ``is_creating=True``)
# has its own independent budget -- ``_TUI_READY_TIMEOUT_SECONDS`` in
# ``mngr.agents.tui_utils`` -- and ignores the ``timeout`` argument we
# forward through ``super()``. Worst-case total wait is therefore roughly
# ``start_action duration + (is_creating ? _TUI_READY_TIMEOUT_SECONDS : 0)
# + _READY_SIGNAL_TIMEOUT_SECONDS``.
_READY_SIGNAL_TIMEOUT_SECONDS: Final[float] = 10.0

# Plugin-scoped subdir inside the per-agent state dir. Mirrors how
# ``mngr_claude`` namespaces its files under ``plugin/claude/anthropic/``
# inside ``$MNGR_AGENT_STATE_DIR``; future ``mngr_gemini`` state can land
# alongside the system-settings file here.
_PLUGIN_STATE_SUBDIR: Final[tuple[str, ...]] = ("plugin", "gemini")

# Filename for the mngr-owned settings file that mngr_gemini installs into
# the per-agent state dir. Gemini reads it as system-tier settings, which sit
# at the top of the precedence stack (system > workspace > user). Keeping
# mngr's hooks at that tier means the user's workspace and ``~/.gemini/``
# stay untouched.
_SYSTEM_SETTINGS_FILENAME: Final[str] = "system_settings.json"

# Set in the agent's environment so Gemini reads our settings file as the
# system-tier override. The env-var override is documented at
# https://geminicli.com/docs/cli/enterprise/#system-settings-path-configuration.
_SYSTEM_SETTINGS_PATH_ENV_VAR: Final[str] = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"

# Set so Gemini treats ``work_dir`` as persistently trusted for the session.
# This clears Gemini's "Do you trust this folder?" gate; without it a headless
# launch either refuses to start or consumes the first keystroke sent via tmux
# to accept the dialog. The env var is Gemini's documented automation path
# (see https://geminicli.com/docs/cli/trusted-folders/#headless-and-automated-environments)
# and is paired with ``_SYSTEM_SETTINGS_PATH_ENV_VAR``: the latter points
# Gemini at the system-tier settings file, this one ensures Gemini gets far
# enough into startup to load it. Smoke-tested against Gemini CLI 0.42.0:
# with this env var set, ``--debug`` reports ``Hook registry initialized
# with N hook entries`` (N matches the configured count); without it the
# count drops to 0.
_TRUST_WORKSPACE_ENV_VAR: Final[str] = "GEMINI_CLI_TRUST_WORKSPACE"


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
                "This may indicate Gemini CLI failed to start, the workspace was not trusted "
                f"({_TRUST_WORKSPACE_ENV_VAR}), or the SessionStart hook was not registered "
                f"({_SYSTEM_SETTINGS_PATH_ENV_VAR}).",
            )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the gemini raw-transcript streamer script.

        Always provisioned (per :class:`HasTranscriptMixin`): the raw bytes
        are the source of truth that the common-transcript converter and
        any future tooling read from.
        """
        return {_RAW_TRANSCRIPT_SCRIPT_NAME: _load_gemini_resource_script(_RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the gemini common-transcript converter script."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_gemini_resource_script(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def _get_system_settings_path(self) -> Path:
        """Path to the mngr-owned system-tier settings file for this agent.

        Lives at ``$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json``
        -- not in the user's workspace, not in ``~/.gemini/``. Mirrors the
        plugin-scoped namespacing ``mngr_claude`` uses under
        ``plugin/claude/anthropic/``.
        """
        return self._get_agent_dir().joinpath(*_PLUGIN_STATE_SUBDIR, _SYSTEM_SETTINGS_FILENAME)

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Wire trust + system-settings env vars for the agent.

        ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` points Gemini at the per-agent
        settings file ``provision`` installed, which holds the readiness hook
        at the system tier. This avoids writing any mngr-managed file into
        the user's workspace or ``~/.gemini/``.

        ``GEMINI_CLI_TRUST_WORKSPACE=true`` clears Gemini's "is this folder
        trusted?" gate so headless launches don't refuse to start.
        """
        env_vars[_SYSTEM_SETTINGS_PATH_ENV_VAR] = str(self._get_system_settings_path())
        env_vars[_TRUST_WORKSPACE_ENV_VAR] = "true"

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Install mngr's Gemini settings, the background-tasks supervisor, the raw-transcript streamer, and (optionally) the common-transcript watcher.

        The settings file lives at
        ``$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json`` and is
        pointed to via ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` (see
        ``modify_env_vars``). It is mngr-owned: provision rewrites it from
        scratch every run, so no merge / no clobber-protection is needed. The
        user's workspace and ``~/.gemini/settings.json`` stay untouched.

        The raw-transcript streamer is always provisioned (per
        :class:`HasTranscriptMixin`): it copies Gemini's native session
        JSONL files into the agent state dir so the bytes survive
        ``~/.gemini/tmp/`` cleanup. The common-transcript watcher install
        delegates the enable-flag check to
        :func:`maybe_provision_common_transcript_scripts`; when
        ``agent_config.emit_common_transcript`` is ``False`` only the raw
        streamer is provisioned and ``gemini_background_tasks.sh`` skips
        launching the common watcher via the on-disk ``-x`` check.

        The background-tasks supervisor (``gemini_background_tasks.sh``) is
        always provisioned and is the only background process launched by
        ``assemble_command``; it owns the lifecycle of both watchers
        (pidfile dedup, EXIT-trap cleanup, restart-on-death). Mirrors
        ``mngr_claude``'s ``claude_background_tasks.sh`` pattern.
        """
        self._install_system_settings(host)
        with mngr_ctx.concurrency_group.make_concurrency_group("gemini_provisioning") as concurrency_group:
            provision_raw_transcript_scripts(
                self,
                host,
                self._get_agent_dir(),
                concurrency_group,
            )
            maybe_provision_common_transcript_scripts(
                self,
                host,
                self._get_agent_dir(),
                concurrency_group,
            )
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {_BACKGROUND_TASKS_SCRIPT_NAME: _load_gemini_resource_script(_BACKGROUND_TASKS_SCRIPT_NAME)},
                concurrency_group,
            )

    def _install_system_settings(self, host: OnlineHostInterface) -> None:
        """Write the per-agent system-tier settings file with the configured hooks.

        Uses ``merge_hooks_config`` rather than ``dict.update`` so a future
        builder that shares an event key (e.g. a second ``SessionStart``
        hook) appends a matcher group instead of silently overwriting the
        readiness sentinel. The ``merged is not None`` invariant holds
        because the current builders target disjoint hook events
        (``SessionStart`` vs ``BeforeTool``), so neither merge encounters a
        pre-existing matcher group with the same matcher and commands. If a
        future builder is added that shares an event key with an earlier one,
        this assertion will need to be reconsidered.
        """
        builders = [build_readiness_hooks_config()]
        if self.agent_config.auto_allow_permissions:
            builders.append(build_permission_auto_allow_hooks_config())

        settings: dict[str, Any] = {}
        for builder_output in builders:
            merged = merge_hooks_config(settings, builder_output)
            assert merged is not None
            settings = merged

        host.write_text_file(self._get_system_settings_path(), serialize_gemini_settings(settings))

    def _build_background_tasks_command(self) -> str:
        """Build the shell command that launches the gemini background-tasks supervisor.

        Mirrors ``ClaudeAgent._build_background_tasks_command``: a single
        backgrounded subshell that owns the lifecycle of every watcher
        (raw streamer + optional common watcher). The supervisor's pidfile
        dedup guarantees that re-running ``assemble_command`` (e.g. on agent
        restart) does not pile up orphaned watcher processes racing on the
        same offset files and output file.
        """
        script_path = f"$MNGR_AGENT_STATE_DIR/commands/{_BACKGROUND_TASKS_SCRIPT_NAME}"
        return f"( bash {script_path} {shlex.quote(self.session_name)} ) &"

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the gemini command with stale-sentinel cleanup and the background-tasks supervisor.

        Inserts ``rm -f $MNGR_AGENT_STATE_DIR/session_started`` as a
        foreground step that runs to completion before ``gemini`` launches,
        so that a leftover sentinel from a previous run cannot make
        ``wait_for_ready_signal`` succeed before the new Gemini session has
        actually started (relevant on every restart, not just first launch).

        The background-tasks supervisor is launched as a single backgrounded
        subshell *before* the ``rm`` step. It owns both watchers (raw
        streamer, optional common converter) with pidfile dedup and
        restart-on-death; placing the watchers under a supervisor (rather
        than firing them directly as separate ``( ... ) &`` subshells)
        prevents accumulation of orphaned watcher processes across agent
        restarts, matching the structure of
        ``mngr_claude``'s ``assemble_command``.

        Placement matters: ``A && B & C`` in bash parses as ``( A && B ) &``
        followed by ``C``, so writing ``rm -f X && ( supervisor ) & gemini``
        would push the ``rm`` into the background where it races gemini's
        startup. Putting the supervisor first confines ``&`` to its subshell
        and leaves ``rm -f X && gemini`` as a foreground sequential chain.

        Bash does not propagate SIGHUP to background children of
        non-interactive shells by default, so the supervisor may outlive the
        tmux session; it polls ``tmux has-session`` and exits cleanly once
        the session is gone.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        clear_sentinel = f"rm -f $MNGR_AGENT_STATE_DIR/{_READINESS_SENTINEL_FILENAME}"
        background_cmd = self._build_background_tasks_command()
        return CommandString(f"{background_cmd} {clear_sentinel} && {base_command}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
