from __future__ import annotations

import importlib.resources
import json
from typing import Any
from typing import ClassVar
from typing import Mapping

from loguru import logger
from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import provision_common_transcript_scripts
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_and_poll_for_cleared_indicator
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr_gemini import resources as _gemini_resources
from imbue.mngr_gemini.gemini_config import GeminiSettingsCorruptError
from imbue.mngr_gemini.gemini_config import build_readiness_hooks_config
from imbue.mngr_gemini.gemini_config import merge_hooks_config

_COMMON_TRANSCRIPT_SCRIPT_NAME = "common_transcript.sh"

# Set in the agent's environment so Gemini treats ``work_dir`` as persistently
# trusted for the session. Smoke-testing against Gemini CLI 0.42.0 showed that
# without this (or an entry in ``~/.gemini/trustedFolders.json``), workspace
# ``.gemini/settings.json`` hooks are silently stripped: ``Hook registry
# initialized with 0 hook entries``. The previous ``--skip-trust`` CLI flag
# only trusted tool execution, not hook registration.
# See https://geminicli.com/docs/cli/trusted-folders/#headless-and-automated-environments.
_TRUST_WORKSPACE_ENV_VAR = "GEMINI_CLI_TRUST_WORKSPACE"


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
        description="Additional CLI arguments to pass to the gemini agent. "
        "The previous default of ('--skip-trust',) was dropped: that flag only "
        "trusts the workspace for tool execution, not hook registration, so "
        "the readiness hook installed by provision() would never fire. Trust "
        "is now established via GEMINI_CLI_TRUST_WORKSPACE=true on the agent's "
        "environment (see modify_env_vars).",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript at "
        "events/gemini/common_transcript/events.jsonl. When enabled, a background "
        "process polls gemini's session JSONL files and converts user, assistant, "
        "tool-call, and tool-result events into the common schema that "
        "`mngr transcript` reads.",
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

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the gemini transcript converter script."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_gemini_resource_script(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Mark ``work_dir`` as trusted for this session so workspace hooks fire.

        Without this env var, Gemini CLI treats workspace ``.gemini/settings.json``
        as untrusted in headless mode: settings load successfully but every hook
        defined in them is dropped from the registry, so the readiness sentinel
        installed by ``provision`` never appears.
        """
        env_vars[_TRUST_WORKSPACE_ENV_VAR] = "true"

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Install workspace settings (readiness hook) and the transcript watcher.

        Two steps:
          1. Install/refresh the readiness hook in ``<work_dir>/.gemini/settings.json``.
             ``mngr`` reads ``$MNGR_AGENT_STATE_DIR/session_started`` to detect
             that the agent's TUI is ready, rather than polling for a banner.
          2. If ``agent_config.emit_common_transcript`` is True, install the
             common-transcript watcher script.

        The hook install is unconditional: the readiness sentinel costs nothing
        when nobody polls for it, and skipping it would only cause ``mngr`` to
        fall back to TUI-banner polling.
        """
        self._install_workspace_hooks(host)
        if not self.agent_config.emit_common_transcript:
            return
        with mngr_ctx.concurrency_group.make_concurrency_group("gemini_provisioning") as concurrency_group:
            provision_common_transcript_scripts(
                host,
                self._get_agent_dir(),
                self.get_common_transcript_scripts(),
                concurrency_group,
            )

    def _install_workspace_hooks(self, host: OnlineHostInterface) -> None:
        """Merge the readiness hook into ``<work_dir>/.gemini/settings.json``.

        Existing user-managed entries (other hook events, mcpServers, approval
        mode, etc.) are preserved by ``merge_hooks_config``: only new matcher
        groups are appended, and re-running this method is a no-op once the
        readiness hook is present. If the file does not yet exist, it is
        created with just the readiness ``hooks`` block.

        If the file exists but cannot be parsed as a JSON object (malformed,
        empty-after-whitespace-only, top-level list/string/etc.) we raise
        ``GeminiSettingsCorruptError`` rather than overwriting it. Gemini has
        no ``settings.local.json`` sidecar that would let us write somewhere
        the user doesn't own, so the safe move is to surface the problem.
        """
        settings_path = self.work_dir / ".gemini" / "settings.json"

        try:
            existing_text = host.read_text_file(settings_path)
        except FileNotFoundError:
            existing_text = ""

        existing_settings: dict[str, Any]
        if not existing_text.strip():
            existing_settings = {}
        else:
            try:
                parsed = json.loads(existing_text)
            except json.JSONDecodeError as exc:
                raise GeminiSettingsCorruptError(str(settings_path), f"invalid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise GeminiSettingsCorruptError(
                    str(settings_path), f"top-level value is {type(parsed).__name__}, not object"
                )
            existing_settings = parsed

        merged = merge_hooks_config(existing_settings, build_readiness_hooks_config())
        if merged is None:
            logger.debug("Readiness hook already configured in {}", settings_path)
            return

        host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the gemini command, prefixing the transcript watcher if enabled.

        The watcher runs as a backgrounded child of the tmux command shell;
        when the tmux session terminates the child dies via SIGHUP propagation.
        When ``agent_config.emit_common_transcript`` is ``False`` the watcher
        is not prepended and the returned command is the base assembled by the
        superclass.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        if not self.agent_config.emit_common_transcript:
            return base_command
        background_cmd = f"( bash $MNGR_AGENT_STATE_DIR/commands/{_COMMON_TRANSCRIPT_SCRIPT_NAME} ) &"
        return CommandString(f"{background_cmd} {base_command}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
