from datetime import datetime
from datetime import timezone
from typing import Any
from typing import cast

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
from imbue.mngr.interfaces.agent import SupportsKeyChordMixin
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.agent import require_key_chord_agent


class DummyCompactionAgent(HasCompactionMixin):
    name = "dummy-compaction"
    agent_type = "dummy"

    def __init__(self) -> None:
        self.compaction_requested = False
        self.last_instructions: str | None = None

    def request_compaction(self, instructions: str | None = None) -> None:
        self.compaction_requested = True
        self.last_instructions = instructions

    def get_cache_ttl_minutes(self) -> int | None:
        return 60

    def get_context_tokens(self) -> int | None:
        return 42000

    def get_idle_since(self) -> datetime | None:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


class DummyDefaultCompactionAgent(HasCompactionMixin):
    name = "dummy-default"
    agent_type = "dummy"

    def request_compaction(self, instructions: str | None = None) -> None:
        pass


class DummyNonCompactionAgent:
    name = "dummy-non-compaction"
    agent_type = "command"


class DummyInteractiveAgent(InteractiveAgentMixin):
    name = "dummy-interactive"
    agent_type = "interactive"

    def send_message(self, message: str) -> None:
        pass


class DummyKeyChordAgent(SupportsKeyChordMixin):
    name = "dummy-chord"
    agent_type = "chord"

    def press_key_chord(self, key: str) -> None:
        pass


def test_has_compaction_mixin_default_methods() -> None:
    agent = DummyDefaultCompactionAgent()
    assert agent.get_cache_ttl_minutes() is None
    assert agent.get_context_tokens() is None
    assert agent.get_idle_since() is None


def test_has_compaction_mixin_implemented_methods() -> None:
    agent = DummyCompactionAgent()
    assert agent.get_cache_ttl_minutes() == 60
    assert agent.get_context_tokens() == 42000
    assert agent.get_idle_since() == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_has_compaction_mixin_request_compaction_with_instructions() -> None:
    agent = DummyCompactionAgent()
    agent.request_compaction(instructions="Focus on the failing unit tests")
    assert agent.compaction_requested is True
    assert agent.last_instructions == "Focus on the failing unit tests"


def test_has_compaction_mixin_request_compaction_without_instructions() -> None:
    agent = DummyCompactionAgent()
    agent.request_compaction()
    assert agent.compaction_requested is True
    assert agent.last_instructions is None


def test_has_compaction_mixin_unsupported_instructions_silently_ignored() -> None:
    # DummyDefaultCompactionAgent does not support instructions and silently ignores them
    agent = DummyDefaultCompactionAgent()
    agent.request_compaction(instructions="Some instruction")


def test_require_compaction_agent_success() -> None:
    compaction_agent = DummyCompactionAgent()
    assert require_compaction_agent(cast(Any, compaction_agent)) is compaction_agent


def test_require_compaction_agent_failure() -> None:
    agent = DummyNonCompactionAgent()
    with pytest.raises(MngrError, match="does not support context compaction"):
        require_compaction_agent(cast(Any, agent))


def test_require_interactive_agent() -> None:
    interactive_agent = DummyInteractiveAgent()
    assert require_interactive_agent(cast(Any, interactive_agent)) is interactive_agent

    non_interactive = DummyNonCompactionAgent()
    with pytest.raises(SendMessageError, match="does not accept interactive messages"):
        require_interactive_agent(cast(Any, non_interactive))


def test_require_key_chord_agent() -> None:
    chord_agent = DummyKeyChordAgent()
    assert require_key_chord_agent(cast(Any, chord_agent)) is chord_agent

    non_chord = DummyNonCompactionAgent()
    with pytest.raises(SendMessageError, match="does not accept key chords"):
        require_key_chord_agent(cast(Any, non_chord))
