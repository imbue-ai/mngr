"""``mngr_antigravity`` plugin -- registers the ``antigravity`` agent type for Google's Antigravity CLI (``agy``).

Antigravity replaced Gemini CLI on 2026-05-19; the legacy request path turns
off for paid-tier accounts on 2026-06-18. Despite the Gemini lineage the new
CLI is architecturally closer to Claude Code than to Gemini -- hook event
names, ``--dangerously-skip-permissions`` flag spelling, and permission-
dialog phrasing all match Claude's surface. The structural choices below
reflect that: process name is the Go binary ``agy``; ``auto_allow_permissions``
is wired through Antigravity's documented ``--dangerously-skip-permissions``
flag rather than a permission hook, since the hook JSON schema is not yet
empirically validated against an authenticated session.

Capabilities deliberately scoped out of v0:

* No readiness sentinel hook -- ``InteractiveTuiAgent``'s banner-poll is the
  sole readiness signal. Live testing against ``agy`` 1.0.0 showed that
  hooks.json is loaded (``hooks_manager.go:45 loaded N named hooks``) but
  hook *execution* is gated behind the ``json-hooks-enabled`` experiment
  flag, which Google must enable per-account. Re-introduce when the
  experiment ships GA.

Transcript support: enabled by default. ``stream_transcript.sh`` tails agy's
per-conversation JSONL files at
``~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl``,
filtered to conversation IDs that *this* agent created (discovered by
grepping agy's own ``--log-file``). ``common_transcript.sh`` converts to
the agent-agnostic schema that ``mngr transcript`` reads.
"""

from __future__ import annotations

import importlib.resources
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr_antigravity import resources as _antigravity_resources
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings

# Top-level CLI flag exposed by `agy --help`; auto-approves every tool call.
# Same spelling as Claude Code's flag.
_DANGEROUSLY_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"

# Supervisor script provisioned into the agent's commands/ dir; owns the
# lifecycle of the raw streamer and (when enabled) the common-transcript
# converter. Mirrors the mngr_claude background-tasks pattern.
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "antigravity_background_tasks.sh"

# Env var consumed by stream_transcript.sh to locate agy's --log-file. We
# also pass `--log-file <path>` to agy itself in ``assemble_command`` so
# the conversation-id discovery has something to grep against.
_AGY_LOG_FILE_ENV_VAR: Final[str] = "ANTIGRAVITY_AGY_LOG_FILE"

# Relative path under $MNGR_AGENT_STATE_DIR for the agy --log-file. Keeping
# it under logs/ groups it with the other per-agent log artifacts.
_AGY_LOG_FILE_RELATIVE_PATH: Final[str] = "logs/agy_cli.log"


def _load_antigravity_resource_script(filename: str) -> str:
    """Load a resource script from the mngr_antigravity resources package."""
    resource_files = importlib.resources.files(_antigravity_resources)
    return resource_files.joinpath(filename).read_text()


class AntigravityAgentConfig(AgentTypeConfig):
    """Config for the antigravity agent type."""

    command: CommandString = Field(
        default=CommandString("agy"),
        description="Command to run the antigravity agent. The Antigravity 2.0 desktop app "
        "ships its own `agy` shim that can shadow the CLI in PATH; if both are installed, "
        "remove the desktop app's `bin/agy` or override this field with the absolute path "
        "to the standalone Go binary.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the antigravity agent.",
    )
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, launch with `--dangerously-skip-permissions` so every tool "
        "call is auto-approved without prompting. Antigravity exposes this as a top-level "
        "CLI flag (same spelling Claude Code uses); we wire it instead of a permission "
        "hook because hook execution is currently gated behind the `json-hooks-enabled` "
        "experiment flag.",
    )
    pre_trust_workspace: bool = Field(
        default=True,
        description="When True, append the agent's work_dir to "
        "`~/.gemini/antigravity-cli/settings.json::trustedWorkspaces` during provisioning "
        "so the first-launch trust dialog is suppressed. The user's settings file is "
        "merged additively -- no other keys are touched and an already-trusted path is "
        "left alone. Disable to fall back to the interactive dialog (useful if you want "
        "the user to consciously accept each new agent's workspace).",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript at "
        "events/antigravity/common_transcript/events.jsonl. When enabled, a background "
        "process tails agy's per-conversation JSONL transcripts and converts user, "
        "assistant, and tool-call/result events into the common schema that "
        "`mngr transcript` reads. The raw transcript at "
        "logs/antigravity_transcript/events.jsonl is always captured (required by "
        "HasTranscriptMixin); only the common-format converter is gated by this flag.",
    )


class AntigravityAgent(InteractiveTuiAgent[AntigravityAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for Google's Antigravity CLI (``agy``)."""

    # Stable substring of the splash banner that the Antigravity TUI renders
    # once startup completes. Polled by ``InteractiveTuiAgent.wait_for_ready_signal``.
    # Captured live from `agy` 1.0.0; the full string is "Antigravity CLI <version>".
    TUI_READY_INDICATOR: ClassVar[str] = "Antigravity CLI"

    def get_expected_process_name(self) -> str:
        # `agy` is a single-file Go binary; ps/tmux show the literal command name.
        return "agy"

    def _send_enter_and_validate(self, tmux_target: str) -> None:
        # Antigravity has no ``UserPromptSubmit`` analog (so the tmux wait-for
        # hook trick Claude uses doesn't apply) and its input row has no
        # placeholder that hides while text is typed and reappears after
        # submission (unlike Gemini's "Type your message"), so we can't poll
        # for a cleared indicator either. ``wait_for_paste_visible`` upstream
        # already confirmed the message landed in the pane before we get here,
        # so a best-effort Enter is the right strategy.
        send_enter_best_effort(self, tmux_target)

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity raw-transcript streamer.

        Always provisioned per :class:`HasTranscriptMixin`: the raw bytes are
        the source of truth that the common-transcript converter and any
        future tooling read from.
        """
        return {_RAW_TRANSCRIPT_SCRIPT_NAME: _load_antigravity_resource_script(_RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity common-transcript converter."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_antigravity_resource_script(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def _get_agy_log_file_path(self) -> Path:
        """Path agy is told to write its --log-file to.

        Lives under the agent's state dir so it is per-agent and durable.
        The streamer reads this file to discover which conversation IDs
        belong to this agent.
        """
        return self._get_agent_dir() / _AGY_LOG_FILE_RELATIVE_PATH

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Expose the agy --log-file path to stream_transcript.sh.

        The streamer needs to grep agy's own log for ``Created conversation
        <uuid>`` lines to scope its watch to this agent's conversations.
        Setting the env var here keeps the script-side path consistent with
        the value we pass to ``agy --log-file`` in ``assemble_command``.
        """
        env_vars[_AGY_LOG_FILE_ENV_VAR] = str(self._get_agy_log_file_path())

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Pre-trust ``work_dir``, then install the background-tasks supervisor + transcript scripts.

        The trust step matches what Claude Code does for its workspace-trust
        gate (writes to a settings file before launch). The transcript scripts
        are then installed under ``$MNGR_AGENT_STATE_DIR/commands/`` so the
        backgrounded supervisor (launched from ``assemble_command``) can
        invoke them.
        """
        if self.agent_config.pre_trust_workspace:
            self._pre_trust_work_dir(host)
        with mngr_ctx.concurrency_group.make_concurrency_group("antigravity_provisioning") as concurrency_group:
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
                {_BACKGROUND_TASKS_SCRIPT_NAME: _load_antigravity_resource_script(_BACKGROUND_TASKS_SCRIPT_NAME)},
                concurrency_group,
            )

    def _pre_trust_work_dir(self, host: OnlineHostInterface) -> None:
        settings_path = get_antigravity_user_settings_path()
        existing_settings = read_antigravity_settings(host, settings_path)
        workspace_path = str(self.work_dir)
        merged = merge_trusted_workspace(existing_settings, workspace_path)
        if merged is None:
            logger.debug("Workspace {} already trusted in {}", workspace_path, settings_path)
            return
        with log_span("Pre-trusting workspace {} in {}", workspace_path, settings_path):
            host.write_text_file(settings_path, serialize_antigravity_settings(merged))

    def _build_background_tasks_command(self) -> str:
        """Shell snippet that launches the background-tasks supervisor.

        Identical structure to mngr_claude's: one backgrounded subshell that
        owns the lifecycle of every watcher (pidfile-deduped, restart-on-
        death). Re-running ``assemble_command`` (e.g. on agent restart) is
        therefore safe because the supervisor's pidfile check causes a
        duplicate launch to exit immediately.
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
        """Build the full launch command.

        Composition (left to right):

        1. ``( bash background_tasks.sh <session> ) &`` -- backgrounded
           supervisor for the transcript streamer + converter.
        2. ``agy <user_args> --log-file <state>/logs/agy_cli.log
           [--dangerously-skip-permissions]`` -- foreground process.

        Bash precedence note: ``A & B`` runs A in the background and B in
        the foreground, so the supervisor's subshell is naturally scoped to
        ``&`` and ``agy`` stays in the foreground. No ``&&`` chain is
        needed because the supervisor's pidfile check tolerates being
        launched before agy is ready.

        The ``--log-file`` arg pipes agy's internal log to a per-agent
        path; stream_transcript.sh greps it for ``Created conversation
        <uuid>`` to scope its watch to this agent. We append the flag
        unconditionally and mngr ensures the directory exists via the
        supervisor's ``mkdir -p`` in resources/.
        """
        log_file_arg = f"--log-file {shlex.quote(str(self._get_agy_log_file_path()))}"
        extra_args: list[str] = [log_file_arg]
        if self.agent_config.auto_allow_permissions:
            extra_args.append(_DANGEROUSLY_SKIP_PERMISSIONS_FLAG)
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        background_cmd = self._build_background_tasks_command()
        return CommandString(f"{background_cmd} {base_command} {' '.join(extra_args)}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the antigravity agent type."""
    return ("antigravity", AntigravityAgent, AntigravityAgentConfig)
