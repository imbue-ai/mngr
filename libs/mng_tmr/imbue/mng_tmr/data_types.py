"""Data types for the test-mapreduce plugin."""

from enum import Enum

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName


class TestOutcome(str, Enum):
    """Outcome of running a single test via an agent."""

    PENDING = "PENDING"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    FIX_TEST_SUCCEEDED = "FIX_TEST_SUCCEEDED"
    FIX_TEST_FAILED = "FIX_TEST_FAILED"
    FIX_IMPL_SUCCEEDED = "FIX_IMPL_SUCCEEDED"
    FIX_IMPL_FAILED = "FIX_IMPL_FAILED"
    FIX_UNCERTAIN = "FIX_UNCERTAIN"
    TIMED_OUT = "TIMED_OUT"
    AGENT_ERROR = "AGENT_ERROR"


class TestResult(FrozenModel):
    """Result reported by a test agent, read from result.json."""

    outcome: TestOutcome = Field(description="The outcome of running and optionally fixing the test")
    summary: str = Field(description="Short human-readable summary of what happened")


class TestAgentInfo(FrozenModel):
    """Tracks a launched test agent and its associated test."""

    test_node_id: str = Field(description="The pytest node ID for the test (e.g. tests/test_foo.py::test_bar)")
    agent_id: AgentId = Field(description="The ID of the launched agent")
    agent_name: AgentName = Field(description="The name of the launched agent")


class TestMapReduceResult(FrozenModel):
    """Aggregated result of the entire test-mapreduce run."""

    test_node_id: str = Field(description="The pytest node ID for the test")
    agent_name: AgentName = Field(description="Name of the agent that ran this test")
    outcome: TestOutcome = Field(description="The final outcome")
    summary: str = Field(description="Short summary from the agent")
    branch_name: str | None = Field(
        default=None,
        description="Git branch name if code changes were pulled, or None",
    )
