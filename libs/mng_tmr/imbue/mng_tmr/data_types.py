"""Data types for the test-mapreduce plugin."""

from enum import auto
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName


class ChangeKind(UpperCaseStrEnum):
    """What kind of change the agent attempted."""

    IMPROVE_TEST = auto()
    FIX_TEST = auto()
    FIX_IMPL = auto()
    FIX_TUTORIAL = auto()


class ChangeStatus(UpperCaseStrEnum):
    """Whether the change succeeded."""

    SUCCEEDED = auto()
    FAILED = auto()
    BLOCKED = auto()


class Change(FrozenModel):
    """One change the agent attempted."""

    status: ChangeStatus = Field(description="Whether the change succeeded, failed, or is blocked")
    summary_markdown: str = Field(description="Markdown description of what was done or attempted")


class DisplayCategory(UpperCaseStrEnum):
    """Derived display category for HTML report grouping and coloring."""

    PENDING = auto()
    FIXED = auto()
    REGRESSED = auto()
    STUCK = auto()
    ERRORED = auto()
    CLEAN_PASS = auto()


class TestResult(FrozenModel):
    """Result reported by a test agent, read from result.json."""

    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(
        default=False, description="Whether an infrastructure error prevented the agent from working"
    )
    tests_passing_before: bool | None = Field(
        default=None, description="Were tests passing before any changes? None if unknown."
    )
    tests_passing_after: bool | None = Field(
        default=None, description="Are tests passing after all changes? None if unknown."
    )
    summary_markdown: str = Field(default="", description="Overall markdown summary of what happened")


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


class IntegratorResult(FrozenModel):
    """Result from the integrator agent that merges fix branches."""

    merged: tuple[str, ...] = Field(default=(), description="Branch names successfully merged")
    failed: tuple[str, ...] = Field(default=(), description="Branch names that could not be merged")
    branch_name: str | None = Field(default=None, description="Integrated branch name, if any merges succeeded")
    summary_markdown: str = Field(default="", description="Markdown summary from the integrator agent")


class TestMapReduceResult(FrozenModel):
    """Result for one test in the map-reduce run."""

    test_node_id: str = Field(description="The pytest node ID for the test")
    agent_name: AgentName = Field(description="Name of the agent that ran this test")
    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(default=False, description="Whether an error prevented the agent from working")
    tests_passing_before: bool | None = Field(default=None, description="Were tests passing before changes?")
    tests_passing_after: bool | None = Field(default=None, description="Are tests passing after changes?")
    summary_markdown: str = Field(default="", description="Markdown summary from the agent")
    branch_name: str | None = Field(
        default=None,
        description="Git branch name if code changes were pulled, or None",
    )
