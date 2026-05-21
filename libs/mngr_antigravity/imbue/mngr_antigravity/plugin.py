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

Capabilities deliberately scoped out of v0 (see plan + spike notes):

* No readiness sentinel hook -- ``InteractiveTuiAgent``'s banner-poll is the
  sole readiness signal. Re-introduce once an authenticated session lets us
  confirm the Antigravity hook schema and ``SessionStart`` timing.
* No ``mngr transcript`` support -- Antigravity stores conversations as
  protobuf ``.pb`` files whose schema we cannot fixture without an
  authenticated CLI. Tracked as follow-up.
"""

from __future__ import annotations

from typing import ClassVar
from typing import Final

from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString

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
        "hook because the Antigravity hook JSON schema has not yet been empirically "
        "validated against an authenticated session.",
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
