"""Non-fixture test utilities for mng-changeling-chat.

Factory functions, helpers, and concrete test implementations that are
explicitly imported by test files.
"""

import json

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.hosts.host import Host
from imbue.mng_changeling_chat.api import get_agent_state_dir


class TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name."""

    def get_expected_process_name(self) -> str:
        return "test-process"


def create_conversation_events(
    host: Host,
    agent: TestAgent,
    conversations: list[dict[str, str]],
) -> None:
    """Create conversation event files on the host for testing."""
    agent_state_dir = get_agent_state_dir(agent, host)
    conv_dir = agent_state_dir / "events" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for conv in conversations:
        lines.append(json.dumps(conv))
    (conv_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


def create_message_events(
    host: Host,
    agent: TestAgent,
    messages: list[dict[str, str]],
) -> None:
    """Create message event files on the host for testing."""
    agent_state_dir = get_agent_state_dir(agent, host)
    msg_dir = agent_state_dir / "events" / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for msg in messages:
        lines.append(json.dumps(msg))
    (msg_dir / "events.jsonl").write_text("\n".join(lines) + "\n")
