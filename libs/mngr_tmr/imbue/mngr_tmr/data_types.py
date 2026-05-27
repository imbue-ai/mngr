"""Test-mapreduce-specific data types.

These types describe what the testing agents and the reducer (integrator)
agent put in their outputs archives, plus the reporter's grouping/coloring
scheme. Framework-side types (``MapReduceContext``, ``AgentMetadata``,
``AgentKind``, etc.) live in ``imbue.mngr_mapreduce.data_types``.
"""

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentName


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
    """Derived section for HTML report grouping and coloring.

    BLOCKED is reserved for results where the coding agent itself decided
    the work was too complex (i.e. produced changes whose status is BLOCKED).
    FAILED is reserved for infrastructure failures: launch failures, agent
    timeouts, missing details, etc. -- cases where the agent never had a
    chance to produce a real verdict.
    """

    NON_IMPL_FIXES = auto()
    IMPL_FIXES = auto()
    BLOCKED = auto()
    FAILED = auto()
    CLEAN_PASS = auto()
    RUNNING = auto()


class TestRunInfo(FrozenModel):
    """Metadata for a single test run within an agent's work."""

    run_name: str = Field(description="The --mngr-e2e-run-name value used for this run")
    description_markdown: str = Field(description="Brief description of what this run was for")


class TestResult(FrozenModel):
    """Result reported by a test agent, read from its outcome JSON."""

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
