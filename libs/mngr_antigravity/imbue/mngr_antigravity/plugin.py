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
* No ``mngr transcript`` support yet -- the JSONL transcript file is at a
  known path (``~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/
  logs/transcript.jsonl``) but plumbing it through mngr's streamer
  infrastructure is deferred to a follow-up commit on this branch.
"""

from __future__ import annotations

from typing import ClassVar
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings

# Top-level CLI flag exposed by `agy --help`; auto-approves every tool call.
# Same spelling as Claude Code's flag.
_DANGEROUSLY_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"


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


class AntigravityAgent(InteractiveTuiAgent[AntigravityAgentConfig]):
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

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Pre-trust ``work_dir`` in Antigravity's user-tier ``settings.json`` when enabled.

        Without this step, every fresh ``work_dir`` triggers Antigravity's
        "Do you trust the contents of this project?" dialog on first launch
        -- the dialog consumes the first keystroke sent to the tmux pane and
        therefore breaks any automation that pastes an initial message
        immediately after start.

        The trust list lives in a single user-global file. We append to it
        rather than overwriting; an already-trusted path is left alone. This
        is the closest analog to Claude Code's settings-file approach since
        Antigravity exposes no env-var override and no per-workspace
        settings file that the CLI reads at startup.
        """
        if not self.agent_config.pre_trust_workspace:
            return
        self._pre_trust_work_dir(host)

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

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Append ``--dangerously-skip-permissions`` when ``auto_allow_permissions`` is set.

        The flag is a top-level CLI flag (documented in ``agy --help``); position
        doesn't matter, so we append it after the user's ``cli_args`` and
        ``agent_args`` rather than interleaving.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        if self.agent_config.auto_allow_permissions:
            return CommandString(f"{base_command} {_DANGEROUSLY_SKIP_PERMISSIONS_FLAG}")
        return base_command


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the antigravity agent type."""
    return ("antigravity", AntigravityAgent, AntigravityAgentConfig)
