"""Project-level conftest for mngr-pi-coding.

When running tests from libs/mngr_pi_coding/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from pathlib import Path
from typing import Any

import pytest

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

# Inherit mngr's shared plugin test fixtures, including the autouse
# setup_test_mngr_env that redirects HOME to a temp dir so tests cannot
# read or write the real ~/.mngr or ~/.claude.json, plus the log_warnings
# capture fixture used by the on_before_provisioning tests.
register_plugin_test_fixtures(globals())


@pytest.fixture()
def pi_agent(tmp_path: Path) -> PiCodingAgent:
    """Create a minimally-configured PiCodingAgent for testing.

    Construction is bypassed (``__new__`` + ``object.__setattr__``) because the
    full agent constructor wires up infrastructure (tmux, host connection) that
    these unit tests do not exercise; only the attributes the tested methods read
    are populated.
    """
    agent = PiCodingAgent.__new__(PiCodingAgent)
    object.__setattr__(agent, "agent_config", PiCodingAgentConfig())
    # Typed as Any: FakeHost satisfies the OnlineHostInterface the agent expects.
    host: Any = FakeHost(host_dir=tmp_path, is_local=True)
    object.__setattr__(agent, "host", host)
    object.__setattr__(agent, "id", AgentId.generate())
    object.__setattr__(agent, "name", AgentName("test-pi"))
    return agent
