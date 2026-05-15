from __future__ import annotations

import importlib.resources
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import ClassVar

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
from imbue.mngr_gemini.gemini_config import build_permission_auto_allow_hooks_config
from imbue.mngr_gemini.gemini_config import build_readiness_hooks_config
from imbue.mngr_gemini.gemini_config import merge_hooks_config

_COMMON_TRANSCRIPT_SCRIPT_NAME = "common_transcript.sh"

# Plugin-scoped subdir inside the per-agent state dir. Mirrors how
# ``mngr_claude`` namespaces its files under ``plugin/claude/anthropic/``
# inside ``$MNGR_AGENT_STATE_DIR``; future ``mngr_gemini`` state can land
# alongside the system-settings file here.
_PLUGIN_STATE_SUBDIR = ("plugin", "gemini")

# Filename for the mngr-owned settings file that mngr_gemini installs into
# the per-agent state dir. Gemini reads it as system-tier settings, which sit
# at the top of the precedence stack (system > workspace > user). Keeping
# mngr's hooks at that tier means the user's workspace and ``~/.gemini/``
# stay untouched.
_SYSTEM_SETTINGS_FILENAME = "system_settings.json"

# Set in the agent's environment so Gemini reads our settings file as the
# system-tier override. The env-var override is documented at
# https://geminicli.com/docs/cli/enterprise/#system-settings-path-configuration.
_SYSTEM_SETTINGS_PATH_ENV_VAR = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"

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
        "No flags are needed by default: workspace trust is cleared via the "
        "GEMINI_CLI_TRUST_WORKSPACE env var (Gemini's documented headless-"
        "automation trust path, set in modify_env_vars), and the mngr-owned "
        "settings file is loaded via GEMINI_CLI_SYSTEM_SETTINGS_PATH, so "
        "neither `--skip-trust` nor a `--settings` flag is required here.",
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

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the gemini transcript converter script."""
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
        """Install mngr's Gemini settings (readiness hook) and the transcript watcher.

        The settings file lives at
        ``$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json`` and is
        pointed to via ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` (see
        ``modify_env_vars``). It is mngr-owned: provision rewrites it from
        scratch every run, so no merge / no clobber-protection is needed. The
        user's workspace and ``~/.gemini/settings.json`` stay untouched.

        When ``agent_config.emit_common_transcript`` is True, the common-
        transcript watcher script is also installed.
        """
        self._install_system_settings(host)
        if not self.agent_config.emit_common_transcript:
            return
        with mngr_ctx.concurrency_group.make_concurrency_group("gemini_provisioning") as concurrency_group:
            provision_common_transcript_scripts(
                host,
                self._get_agent_dir(),
                self.get_common_transcript_scripts(),
                concurrency_group,
            )

    def _install_system_settings(self, host: OnlineHostInterface) -> None:
        """Write the mngr-owned system-tier settings file with the configured hooks.

        Always includes the readiness hook (``SessionStart`` → touch
        ``$MNGR_AGENT_STATE_DIR/session_started``). When
        ``agent_config.auto_allow_permissions`` is True, also includes a
        ``BeforeTool`` wildcard hook that auto-approves every tool call.
        Because mngr owns this file outright (it lives in the per-agent state
        dir), no read-modify-merge dance against any pre-existing file is
        needed: each provision run rewrites it from scratch. Composition of
        the configured builders still goes through ``merge_hooks_config`` so
        any future builder that adds entries under an already-used event key
        is handled correctly (matcher-group dedup, deep copy, no mutation).
        """
        builders = [build_readiness_hooks_config()]
        if self.agent_config.auto_allow_permissions:
            builders.append(build_permission_auto_allow_hooks_config())

        settings: dict[str, Any] = {}
        for builder_output in builders:
            merged = merge_hooks_config(settings, builder_output)
            # Each builder contributes at least one new matcher group against
            # a fresh accumulator, so merge_hooks_config never returns None.
            assert merged is not None
            settings = merged

        host.write_text_file(self._get_system_settings_path(), json.dumps(settings, indent=2) + "\n")

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the gemini command, prefixing the transcript watcher if enabled.

        The watcher is launched fire-and-forget as a backgrounded subshell
        (``( bash ... ) &``). Bash does not propagate SIGHUP to background
        children of non-interactive shells by default, so the watcher may
        outlive the tmux session and continue polling until killed by host
        teardown or until its session inputs disappear. When
        ``agent_config.emit_common_transcript`` is ``False`` the watcher is
        not prepended and the returned command is the base assembled by the
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
