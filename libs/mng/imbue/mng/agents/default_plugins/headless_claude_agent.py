"""HeadlessClaude agent type for non-interactive Claude usage.

This agent type runs `claude --print` in a tmux session, making headless
claude a first-class citizen of the agent system. Agents are visible in
`mng list`, have state directories, and get destroyed when done.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

from pydantic import Field

from imbue.imbue_common.pure import pure
from imbue.mng import hookimpl
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.errors import SendMessageError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import CommandString


@pure
def extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if parsed.get("type") != "stream_event":
        return None

    event = parsed.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None

    if delta.get("type") != "text_delta":
        return None

    text = delta.get("text")
    if isinstance(text, str):
        return text

    return None


class HeadlessClaudeAgentConfig(ClaudeAgentConfig):
    """Config for the headless_claude agent type.

    Inherits all ClaudeAgentConfig fields (sync settings, credentials, etc.).
    Command defaults to 'claude'.
    """

    command: CommandString = Field(
        default=CommandString("claude"),
        description="Command to run headless claude agent",
    )


class HeadlessClaude(ClaudeAgent):
    """Agent type for non-interactive (headless) Claude usage.

    Runs `claude --print` with stdout redirected to a file so callers can
    read output programmatically via stream_output(). Does not support
    interactive messages, paste detection, or TUI readiness checking.
    """

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless agents do not accept interactive messages."""
        raise SendMessageError(
            str(self.name),
            "Headless claude agents do not accept interactive messages.",
        )

    def uses_paste_detection_send(self) -> bool:
        return False

    def get_tui_ready_indicator(self) -> str | None:
        return None

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        raise NotImplementedError(
            "HeadlessClaude agents do not support wait_for_ready_signal. "
            "The prompt is passed as a CLI arg, not via send_message."
        )

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Build a simplified command for headless operation.

        Always includes --print, no session resumption, no background activity
        tracking. Redirects stdout to $MNG_AGENT_STATE_DIR/stdout.jsonl.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            raise NoCommandDefinedError(f"No command defined for agent type '{self.agent_type}'")

        parts = [base, "--print"]

        all_extra_args = self.agent_config.cli_args + agent_args
        if all_extra_args:
            parts.extend(all_extra_args)

        cmd_str = " ".join(parts)
        stdout_path = "$MNG_AGENT_STATE_DIR/stdout.jsonl"
        return CommandString(f"{cmd_str} > {shlex.quote(stdout_path)}")

    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout.jsonl file for this agent."""
        return self._get_agent_dir() / "stdout.jsonl"

    def stream_output(self) -> Iterator[str]:
        """Stream text output from the headless agent.

        Tails $MNG_AGENT_STATE_DIR/stdout.jsonl, parses stream-json events,
        and yields text delta chunks. Completes when the agent process exits
        and the file is fully consumed.
        """
        stdout_path = self._get_stdout_path()
        offset = 0

        while True:
            try:
                content = self.host.read_text_file(stdout_path)
            except FileNotFoundError:
                # File not created yet -- check if agent is still running
                state = self.get_lifecycle_state()
                if state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE):
                    return
                time.sleep(0.1)
                continue

            new_content = content[offset:]
            offset = len(content)

            if new_content:
                for line in new_content.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    text = extract_text_delta(stripped)
                    if text is not None:
                        yield text

            # Check if agent has finished
            state = self.get_lifecycle_state()
            if state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE):
                # Do one final read to catch any remaining output
                try:
                    content = self.host.read_text_file(stdout_path)
                except FileNotFoundError:
                    return
                final_content = content[offset:]
                if final_content:
                    for line in final_content.splitlines():
                        stripped = line.strip()
                        if not stripped:
                            continue
                        text = extract_text_delta(stripped)
                        if text is not None:
                            yield text
                return

            time.sleep(0.1)


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
