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


class ReportSection(UpperCaseStrEnum):
    """Derived section for HTML report grouping and coloring."""

    NON_IMPL_FIXES = auto()
    IMPL_FIXES = auto()
    BLOCKED = auto()
    CLEAN_PASS = auto()
    RUNNING = auto()


class TestRunInfo(FrozenModel):
    """Metadata for a single test run within an agent's work."""

    run_name: str = Field(description="The --mng-e2e-run-name value used for this run")
    description_markdown: str = Field(description="Brief description of what this run was for")


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
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="List of test runs performed, in order")


class TestAgentInfo(FrozenModel):
    """Tracks a launched test agent and its associated test."""

    test_node_id: str = Field(description="The pytest node ID for the test (e.g. tests/test_foo.py::test_bar)")
    agent_id: AgentId = Field(description="The ID of the launched agent")
    agent_name: AgentName = Field(description="The name of the launched agent")
    branch_name: str | None = Field(default=None, description="Git branch created for this agent")
    created_at: float = Field(description="Monotonic timestamp (time.monotonic()) when the agent was created")


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
    """Result from the integrator agent that cherry-picks fix branches."""

    agent_name: AgentName | None = Field(default=None, description="Name of the integrator agent")
    squashed_branches: tuple[str, ...] = Field(default=(), description="Branches in the squashed non-impl commit")
    squashed_commit_hash: str | None = Field(default=None, description="Commit hash of the squashed non-impl commit")
    impl_priority: tuple[str, ...] = Field(default=(), description="Impl branches in priority order, highest first")
    impl_commit_hashes: dict[str, str] = Field(
        default_factory=dict, description="Mapping of impl branch name to its commit hash on the integrated branch"
    )
    failed: tuple[str, ...] = Field(default=(), description="Branch names that could not be integrated")
    branch_name: str | None = Field(default=None, description="Integrated branch name, if any merges succeeded")


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
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="Test runs performed by the agent, in order")
