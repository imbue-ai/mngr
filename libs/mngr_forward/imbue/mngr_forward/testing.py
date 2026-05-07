"""Test helpers for mngr_forward unit + integration tests.

Per CLAUDE.md, do not create tests for testing.py itself; the helpers are
exercised through the tests that import them.
"""

from imbue.mngr.primitives import AgentId

# A trio of canned, well-formed agent IDs for use in tests. AgentId is a
# RandomId requiring exactly 32 hex chars after the ``agent-`` prefix; we
# use deterministic constants so test output is stable.
TEST_AGENT_ID_1: AgentId = AgentId("agent-" + "0" * 31 + "1")
TEST_AGENT_ID_2: AgentId = AgentId("agent-" + "0" * 31 + "2")
TEST_AGENT_ID_3: AgentId = AgentId("agent-" + "0" * 31 + "3")
