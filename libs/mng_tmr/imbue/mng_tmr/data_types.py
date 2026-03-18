"""Data types for the test-mapreduce plugin."""

from enum import Enum
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName


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


class TmrLaunchConfig(FrozenModel):
    """Common configuration for launching tmr agents."""

    source_dir: Path = Field(description="Source directory for agent work dirs")
    source_host: OnlineHostInterface = Field(description="Local host where source code lives")
    agent_type: AgentTypeName = Field(description="Type of agent to run (claude, codex, etc.)")
    provider_name: ProviderInstanceName = Field(description="Provider for agent hosts (local, docker, modal)")
    env_options: AgentEnvironmentOptions = Field(
        default_factory=AgentEnvironmentOptions,
        description="Environment variables to pass to agents",
    )
    label_options: AgentLabelOptions = Field(
        default_factory=AgentLabelOptions,
        description="Labels to attach to agents",
    )
    snapshot: SnapshotName | None = Field(
        default=None,
        description="Snapshot to use for host creation (None means build from scratch)",
    )


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
