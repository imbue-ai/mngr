"""Data types for the mngr-mapreduce framework.

Defines the abstract ``MapReduceRecipe`` interface that callers implement, the
runtime context the framework threads through every recipe hook, and the
internal bookkeeping models (agent infos, launch config, agent kind,
per-agent metadata) the orchestrator carries between phases.

The recipe interface is intentionally small: ``discover`` produces the task
list, ``build_mapper_prompt`` / ``build_reducer_prompt`` produce the prompts,
``on_mapper_finalized`` / ``on_reducer_finalized`` are the only points where
recipe-specific knowledge looks at the just-extracted outputs archive, and
``render_report`` writes the HTML.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName


class MapReduceTask(FrozenModel):
    """One unit of work in a map-reduce run.

    ``id`` is the opaque identifier the recipe will see again in
    ``build_mapper_prompt``; ``display_id`` is the short form used to derive
    the agent name and branch slug (defaults to ``id``).
    """

    id: str = Field(description="Opaque task identifier passed back to build_mapper_prompt")
    display_id: str | None = Field(
        default=None,
        description="Short form used when sanitizing into the agent name and branch slug. Defaults to id.",
    )

    def slug_source(self) -> str:
        return self.display_id if self.display_id is not None else self.id


class AgentKind(UpperCaseStrEnum):
    """Classifies an agent within a map-reduce run.

    The string value is also stamped on each agent as the ``mapreduce_role``
    label so a discovery query (``mngr ls --include 'labels.mapreduce_role ==
    "..."'``) matches the in-process classification.
    """

    MAPPER = auto()
    REDUCER = auto()
    SNAPSHOTTER = auto()


class AgentMetadata(FrozenModel):
    """In-memory hand-off between orchestration and the recipe's renderer.

    Orchestration produces one of these per launched agent (plus one per
    launch failure). The recipe is the authority on what to do with the
    extracted outputs under ``<output_dir>/<agent_name>/``; the framework
    only knows ``agent_name``, optional ``task_id`` / ``branch_name``, and
    whether the agent failed to publish outputs (``error_summary``).
    """

    kind: AgentKind = Field(description="What flavor of map-reduce agent this is")
    agent_name: AgentName = Field(description="Agent name (matches the subdir under output_dir)")
    task_id: str | None = Field(
        default=None,
        description="Task id for mappers; None for the reducer",
    )
    branch_name: str | None = Field(default=None, description="Git branch created for this agent, if any")
    error_summary: str | None = Field(
        default=None,
        description="Markdown to render when the agent did not complete normally (timeout, launch failure, etc.). "
        "When None, the recipe's renderer looks for whatever it expects under output_dir/<agent_name>/.",
    )


class MapperInfo(FrozenModel):
    """Tracks a launched mapper agent and its associated task."""

    task_id: str = Field(description="Opaque id of the task this agent was launched for")
    agent_id: AgentId = Field(description="The ID of the launched agent")
    agent_name: AgentName = Field(description="The name of the launched agent")
    branch_name: str = Field(description="Git branch created for this agent")
    created_at: float = Field(description="Monotonic timestamp (time.monotonic()) when the agent was created")


class ReducerInfo(FrozenModel):
    """Tracks the launched reducer agent.

    There's no ``created_at`` field because the reduce phase has a single
    overall deadline (built from ``reducer_timeout`` at launch time)
    rather than a per-agent timeout.
    """

    agent_id: AgentId = Field(description="The ID of the launched agent")
    agent_name: AgentName = Field(description="The name of the launched agent")
    branch_name: str = Field(description="Git branch created for this agent")


class LaunchConfig(FrozenModel):
    """Common configuration for launching map-reduce agents."""

    source_dir: Path = Field(description="Source directory for agent work dirs")
    source_host: OnlineHostInterface = Field(description="Local host where source code lives")
    base_commit: str = Field(description="Commit at source_dir HEAD when the run started; used as the bundle base")
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
    templates: tuple[str, ...] = Field(
        default=(),
        description="Create template names to apply when creating agents",
    )
    additional_authorized_keys: tuple[str, ...] = Field(
        default=(),
        description="SSH public key lines to install in authorized_keys on each agent host (allows inbound SSH)",
    )


class MapReduceContext(FrozenModel):
    """Runtime context the framework threads through every recipe hook.

    Carries everything the recipe might need from the framework's state:
    the mngr context (for concurrency-group access and config lookups), the
    repo paths, the run name (a UTC YYYYMMDDHHMMSS timestamp the framework
    generates), the output directory, and the parsed output options (so the
    recipe can emit structured events that fit the active output format).
    None of these change during a run.
    """

    mngr_ctx: MngrContext
    source_dir: Path
    run_name: str
    output_dir: Path
    output_opts: OutputOptions

    @property
    def cg(self) -> ConcurrencyGroup:
        return self.mngr_ctx.concurrency_group


class MapReduceRecipe(ABC):
    """A concrete map-reduce specification the framework executes.

    Lifecycle: the framework calls ``discover`` once at the start to get the
    task list, then ``build_mapper_prompt(task)`` for each task it launches.
    As each mapper publishes its outputs archive the framework extracts it
    under ``output_dir/<agent_name>/`` and calls ``on_mapper_finalized``.
    Once every mapper has finished (or timed out), if at least one mapper
    produced an outputs archive, the framework launches a reducer with
    ``build_reducer_prompt()`` (after rsyncing the entire ``output_dir``
    into the reducer's work dir), waits for its outputs, and calls
    ``on_reducer_finalized``. Throughout, ``render_report`` is called
    after every state change to produce the HTML report; if it returns
    a path, the framework best-effort-uploads it to S3.
    """

    # Short identifier for this recipe. Subclasses must override.
    #
    # Used as the prefix in agent names (``<name>-<run>-<slug>``), branch
    # names (``<name>/<run>/<slug>``), and host names. Should be a valid
    # identifier and stable across releases (used in ``mngr ls`` filter
    # expressions to locate prior runs).
    name: ClassVar[str]

    @abstractmethod
    def discover(self, ctx: MapReduceContext) -> list[MapReduceTask]:
        """Produce the task list for this run.

        Called once before any agents are launched. Raise to abort the run.
        """

    @abstractmethod
    def build_mapper_prompt(self, ctx: MapReduceContext, task: MapReduceTask) -> str:
        """Build the initial message sent to the mapper agent for ``task``."""

    @abstractmethod
    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        """Build the message sent to the reducer agent.

        The framework first creates the reducer agent with no initial message,
        rsyncs the local ``output_dir`` into its work dir (under a recipe-
        agnostic subdir), and then sends this prompt as a follow-up message.
        """

    def on_mapper_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: MapperInfo) -> None:
        """Called after a mapper's outputs archive has been extracted.

        ``agent_dir`` is ``ctx.output_dir / info.agent_name``. The agent is
        still alive on its host at this point; it will be stopped immediately
        after this returns. Exceptions are logged by the framework but do
        not abort the run.

        Default impl is a no-op.
        """
        return None

    def on_reducer_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: ReducerInfo) -> None:
        """Called after the reducer's outputs archive has been extracted.

        Same contract as ``on_mapper_finalized``. Default impl is a no-op.
        """
        return None

    @abstractmethod
    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        """Render the HTML report for the current state of the run.

        Called by the framework after every state change (initial launch,
        each mapper finalization, the reducer's finalization). Should be
        idempotent and cheap to call repeatedly. Returns the path to the
        written report, or None to skip rendering this tick; if non-None,
        the framework best-effort-uploads it.
        """
