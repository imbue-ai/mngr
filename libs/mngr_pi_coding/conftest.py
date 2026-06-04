"""Project-level conftest for mngr-pi-coding.

When running tests from libs/mngr_pi_coding/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig

suppress_warnings()
register_conftest_hooks(globals())

# Register the standard mngr plugin test fixtures (the purpose-built helper for
# plugin conftests). This injects the autouse setup_test_mngr_env fixture, which
# redirects HOME to a temp dir so tests cannot read or write the real ~/.pi.
register_plugin_test_fixtures(globals())


@pytest.fixture()
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru warning messages for assertion in tests.

    log_warnings is not part of register_plugin_test_fixtures' standard set (it
    lives in mngr's own conftest), so it is defined here as a plugin-local extra.
    Tolerates handler removal during the test.
    """
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


@pytest.fixture()
def make_pi_agent(tmp_path: Path) -> Callable[..., PiCodingAgent]:
    """Factory for minimally-populated PiCodingAgents.

    Uses pydantic's ``model_construct`` to populate only the fields these unit
    tests read (``agent_config``, ``host``, ``id``, ``name``) and skip both
    validation and the fields they never touch (``work_dir``, ``mngr_ctx``,
    etc.). The real constructor would require a full MngrContext and wire up host
    connections/tmux that are irrelevant here.
    """

    def _make(*, agent_config: PiCodingAgentConfig | None = None, host: Any = None) -> PiCodingAgent:
        return PiCodingAgent.model_construct(
            agent_config=agent_config if agent_config is not None else PiCodingAgentConfig(),
            # FakeHost stands in for the OnlineHostInterface the agent expects.
            host=host if host is not None else FakeHost(host_dir=tmp_path, is_local=True),
            id=AgentId.generate(),
            name=AgentName("test-pi"),
        )

    return _make


@pytest.fixture()
def pi_agent(make_pi_agent: Callable[..., PiCodingAgent]) -> PiCodingAgent:
    """A minimally-populated PiCodingAgent with default config and a local FakeHost."""
    return make_pi_agent()
