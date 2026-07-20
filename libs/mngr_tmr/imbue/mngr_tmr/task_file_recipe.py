"""The task-file map-reduce recipe: fan out a JSONL task file to one agent per task.

Where the test fan-out recipe (:class:`TestMapReduceRecipe`) discovers its
tasks by collecting pytest node ids, this recipe reads them from a task file:
one JSON packet per line, each carrying an opaque ``id``, an optional
``display_id`` (used for agent/branch slugs), a ``kind``, and a free-form
``context`` object that is handed to the caller-supplied mapper prompt
template as pretty-printed JSON. The producer side decides what a task means
(for the minds spec-witnessing flow, ``minds specs plan --for-tmr`` emits one
packet per spec unit); this recipe stays generic.

There are no packaged prompt defaults: the caller passes ``--mapper-prompt``
and ``--reducer-prompt`` templates that anchor on the task semantics.
Branch-bundle retrieval, the HTML report, and the integrator outcome
contract are shared with the test fan-out recipe.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_tmr.branch_bundles import apply_agent_branch_bundle_if_present
from imbue.mngr_tmr.branch_bundles import finalize_reducer_branch
from imbue.mngr_tmr.branch_bundles import reducer_branch_applied
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_task_file_mapper_prompt
from imbue.mngr_tmr.recipe import emit_report_url
from imbue.mngr_tmr.recipe import validate_recipe_name
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report_upload import maybe_upload_report

# The only task-packet schema version this recipe accepts.
TASK_PACKET_SCHEMA_VERSION: Final[int] = 1

_DEFAULT_RECIPE_NAME = "tmr-tasks"


class TaskPacketLoadError(MngrError, ValueError):
    """Raised when a task file cannot be parsed into valid task packets."""

    ...


class TaskPacket(FrozenModel):
    """One task of a task file: an opaque id plus a free-form context for the mapper prompt."""

    schema_version: int = Field(description="Task-packet schema version (must match the recipe's)")
    id: str = Field(min_length=1, description="Opaque task identifier passed back to build_mapper_prompt")
    display_id: str | None = Field(default=None, description="Short form used for agent/branch slugs (defaults to id)")
    kind: str = Field(min_length=1, description="Producer-defined task kind, passed to the mapper prompt")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Producer-defined context object, handed to the mapper prompt template as JSON",
    )


def load_task_packets(tasks_file: Path) -> tuple[TaskPacket, ...]:
    """Parse and validate a JSONL task file. Raises TaskPacketLoadError on any problem."""
    try:
        file_text = tasks_file.read_text(encoding="utf-8")
    except OSError as e:
        raise TaskPacketLoadError(f"Cannot read tasks file: {tasks_file}") from e
    packets: list[TaskPacket] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(file_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw_packet = json.loads(line)
        except json.JSONDecodeError as e:
            raise TaskPacketLoadError(f"{tasks_file}:{line_number}: invalid JSON: {e}") from e
        try:
            packet = TaskPacket.model_validate(raw_packet)
        except ValidationError as e:
            raise TaskPacketLoadError(f"{tasks_file}:{line_number}: invalid task packet: {e}") from e
        if packet.schema_version != TASK_PACKET_SCHEMA_VERSION:
            raise TaskPacketLoadError(
                f"{tasks_file}:{line_number}: unsupported schema_version {packet.schema_version} "
                f"(expected {TASK_PACKET_SCHEMA_VERSION})"
            )
        if packet.id in seen_ids:
            raise TaskPacketLoadError(f"{tasks_file}:{line_number}: duplicate task id '{packet.id}'")
        seen_ids.add(packet.id)
        packets.append(packet)
    if not packets:
        raise TaskPacketLoadError(f"{tasks_file}: no task packets found")
    return tuple(packets)


class TaskFileMapReduceRecipe(MapReduceRecipe, FrozenModel):
    """Run one agent per task of a JSONL task file, integrating their branches with a reducer.

    Mapper prompts come from the caller-supplied template rendered with the
    task's packet (id, kind, and context as JSON). Mapper branches come back
    as ``branch.bundle`` files and are applied to the local source repo; the
    reducer's branch is likewise fetched and emitted. The report reuses the
    shared test-mapreduce HTML report, so mapper agents must write the same
    outcome JSON contract as test-fan-out mappers.
    """

    name: str = Field(
        default=_DEFAULT_RECIPE_NAME,
        description="Variant name; prefixes this run's agent/branch/host names so distinct task files "
        "stay separable and reviewable on their own.",
    )
    packets: tuple[TaskPacket, ...] = Field(description="The validated task packets to fan out")
    tasks_file: Path = Field(description="The task file the packets were loaded from (used in report hints)")
    mapper_prompt_path: Path = Field(description="Mapper prompt template (rendered with the task's packet)")
    reducer_prompt_path: Path = Field(description="Reducer prompt template")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_recipe_name(value)

    def discover(self, ctx: MapReduceContext) -> list[MapReduceTask]:
        return [MapReduceTask(id=packet.id, display_id=packet.display_id) for packet in self.packets]

    def build_mapper_prompt(self, ctx: MapReduceContext, task: MapReduceTask) -> str:
        packet = next((candidate for candidate in self.packets if candidate.id == task.id), None)
        if packet is None:
            raise SwitchError(f"task '{task.id}' is not one of this recipe's packets")
        return build_task_file_mapper_prompt(
            task_id=packet.id,
            kind=packet.kind,
            context_json=json.dumps(packet.context, indent=2, ensure_ascii=False),
            template_path=self.mapper_prompt_path,
        )

    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        return build_integrator_prompt(template_path=self.reducer_prompt_path)

    def on_mapper_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: MapperInfo) -> None:
        apply_agent_branch_bundle_if_present(ctx.source_dir, agent_dir, info.branch_name, str(info.agent_name), ctx.cg)

    def on_reducer_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: ReducerInfo) -> None:
        finalize_reducer_branch(ctx, agent_dir, info)

    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        applied = reducer_branch_applied(ctx.source_dir, reducer, ctx.cg)
        report_path = generate_html_report(
            agents=agents,
            output_dir=ctx.output_dir,
            integrator_metadata=reducer,
            run_commands=self._build_run_commands(
                ctx.run_name,
                integrated_branch=reducer.branch_name if applied and reducer is not None else None,
            ),
        )
        emit_report_url(maybe_upload_report(report_path, ctx.run_name), ctx.output_opts)
        return report_path

    def _build_run_commands(self, run_name: str, integrated_branch: str | None) -> list[tuple[str, str]]:
        """Build the (label, command) hint pairs rendered at the bottom of the report."""
        commands = [
            ("List agents from this run", f"mngr ls --include 'labels.mapreduce_run_name == \"{run_name}\"'"),
            (
                "Reintegrate",
                f"mngr tmr-tasks --tasks-file {self.tasks_file} --mapper-prompt {self.mapper_prompt_path} "
                f"--reducer-prompt {self.reducer_prompt_path} --name {self.name} "
                f"--reintegrate --run-name {run_name}",
            ),
        ]
        if integrated_branch is not None:
            commands.append(("Push integrated branch", f"git push origin {integrated_branch}"))
        return commands
