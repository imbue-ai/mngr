"""Assemble an ATIF-shaped common-transcript stream into a full ATIF trajectory.

The stream (see :mod:`imbue.mngr.agents.common_transcript_records`) is an
append-only JSONL form of ATIF: a ``header`` line, ``step`` lines, and
``observation`` lines whose tool results arrive after the agent step that
issued the calls. This module holds the pure merge logic that turns a stream's
records into a validated vendored :class:`~imbue.mngr.agents.data_types.atif.trajectory.Trajectory`
document, per the merge rules in ``specs/atif-transcript-alignment/spec.md``.
Reading streams from hosts and resolving subagents lives in
:mod:`imbue.mngr.api.trajectory`.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import TypeVar
from typing import assert_never

import pydantic
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.common_transcript_records import CommonTranscriptRecord
from imbue.mngr.agents.common_transcript_records import HeaderRecord
from imbue.mngr.agents.common_transcript_records import LEGACY_RECORD_TYPE_NAMES
from imbue.mngr.agents.common_transcript_records import OLD_FORMAT_STREAM_EXPLANATION
from imbue.mngr.agents.common_transcript_records import ObservationRecord
from imbue.mngr.agents.common_transcript_records import StepRecord
from imbue.mngr.agents.common_transcript_records import StepSource
from imbue.mngr.agents.common_transcript_records import parse_common_transcript_record
from imbue.mngr.agents.data_types.atif.agent import Agent as AtifAgent
from imbue.mngr.agents.data_types.atif.final_metrics import FinalMetrics
from imbue.mngr.agents.data_types.atif.observation import Observation as AtifObservation
from imbue.mngr.agents.data_types.atif.observation_result import ObservationResult as AtifObservationResult
from imbue.mngr.agents.data_types.atif.step import Step as AtifStep
from imbue.mngr.agents.data_types.atif.subagent_trajectory_ref import SubagentTrajectoryRef
from imbue.mngr.agents.data_types.atif.trajectory import Trajectory
from imbue.mngr.errors import InvalidCommonTranscriptRecordError
from imbue.mngr.errors import MalformedJsonlLineError
from imbue.mngr.errors import TrajectoryBuildError
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner

# The values of extra.subagent_kind on subagent refs and embedded trajectories
# (see the spec's "Subagent embedding" section). NATIVE_SUBAGENT_KIND is the spec's
# vocabulary for subagents carved out of a native sidechain transcript; no emitter
# produces such streams yet, so nothing in mngr stamps it today.
MNGR_SUBAGENT_KIND: Final[str] = "mngr"
NATIVE_SUBAGENT_KIND: Final[str] = "native"

_NumberT = TypeVar("_NumberT", int, float)


class TrajectoryEnrichment(FrozenModel):
    """Root-document fields supplied from mngr's own records, not from the stream."""

    agent_name: str = Field(description="ATIF agent.name: the mngr agent type (e.g. 'claude')")
    agent_version: str = Field(description="ATIF agent.version: the plugin/CLI version where known, else 'unknown'")
    session_id: str = Field(description="ATIF session_id: the mngr agent id")
    trajectory_id: str = Field(description="ATIF trajectory_id: a stable per-document id derived from the agent id")


class EmbeddedSubagent(FrozenModel):
    """A resolved subagent trajectory to embed under the delegating tool call."""

    trajectory: Trajectory = Field(description="The subagent's fully built trajectory (must carry a trajectory_id)")
    subagent_kind: str = Field(description="Which resolution produced it: 'mngr' (proxy sibling) or 'native'")


class TrajectoryBuildResult(FrozenModel):
    """A built trajectory plus the non-fatal problems encountered while merging."""

    trajectory: Trajectory = Field(description="The validated assembled ATIF trajectory")
    warnings: tuple[str, ...] = Field(
        description="Non-fatal merge problems (unmatched observation results, skipped or still-pending subagents)"
    )


def parse_stream_content(content: str, source_description: str) -> tuple[CommonTranscriptRecord, ...]:
    """Parse a stream's JSONL content into typed records, in file order.

    Malformed-JSON lines go through :class:`MalformedJsonLineWarner`, so a
    truncated trailing line (a crash mid-append) is dropped silently while an
    interior one is warned about. A line that parses but is not a JSON object,
    or that violates the record schema, is a :class:`TrajectoryBuildError` (the
    emit-time contract promises conformance, so a violation means the stream
    cannot be trusted).
    """
    records: list[CommonTranscriptRecord] = []
    warner = MalformedJsonLineWarner(source_description=source_description)
    for line_idx, line in enumerate(content.splitlines(), start=1):
        try:
            parsed = warner.parse(line)
        except MalformedJsonlLineError as e:
            raise TrajectoryBuildError(f"Line {line_idx} of {source_description} is not a JSON object: {e}") from e
        if parsed is None:
            continue
        raw_record, _raw_line = parsed
        record_type = raw_record.get("type")
        # Caught by name because the retired types are no longer in the schema at all.
        if record_type in LEGACY_RECORD_TYPE_NAMES:
            raise TrajectoryBuildError(
                f"{source_description} contains a retired '{record_type}' record (line {line_idx}), so it was "
                f"written by a pre-ATIF emitter: {OLD_FORMAT_STREAM_EXPLANATION}"
            )
        try:
            records.append(parse_common_transcript_record(raw_record))
        except InvalidCommonTranscriptRecordError as e:
            raise TrajectoryBuildError(
                f"Line {line_idx} of {source_description} violates the record schema: {e}"
            ) from e
    return tuple(records)


class _StepAccumulator(FrozenModel):
    """One stream step with the observation results attached to it so far."""

    record: StepRecord = Field(description="The stream step record")
    attached_results: tuple[AtifObservationResult, ...] = Field(
        description="Streamed results attached in arrival order"
    )

    def with_result(self, result: AtifObservationResult) -> "_StepAccumulator":
        return _StepAccumulator(record=self.record, attached_results=self.attached_results + (result,))


@pure
def _require_header(records: Sequence[CommonTranscriptRecord]) -> HeaderRecord:
    if len(records) == 0:
        raise TrajectoryBuildError("Cannot build a trajectory from an empty stream")
    first_record = records[0]
    if not isinstance(first_record, HeaderRecord):
        raise TrajectoryBuildError(
            f"The stream does not start with a header record (first record type: '{first_record.type}'), so it "
            f"was written by a pre-ATIF emitter: {OLD_FORMAT_STREAM_EXPLANATION}"
        )
    return first_record


@pure
def _plain_result(result: AtifObservationResult) -> AtifObservationResult:
    # Attached results arrive as StreamObservationResult (the stream's narrowed
    # subclass); the built document carries plain vendored results so it compares
    # and round-trips cleanly, and so the record's sub-models are never aliased
    # into the document.
    return AtifObservationResult(
        source_call_id=result.source_call_id,
        content=result.content,
        subagent_trajectory_ref=result.subagent_trajectory_ref,
        extra=result.extra,
    )


@pure
def _redirected_unmatched_result(result: AtifObservationResult) -> AtifObservationResult:
    # The vendored Trajectory model rejects a source_call_id that no tool call in the
    # same step declares, so an unmatched result keeps its id under extra instead.
    return AtifObservationResult(
        source_call_id=None,
        content=result.content,
        subagent_trajectory_ref=result.subagent_trajectory_ref,
        extra={**(result.extra or {}), "unmatched": True, "source_call_id": result.source_call_id},
    )


@pure
def _result_with_subagent_ref(result: AtifObservationResult, subagent: EmbeddedSubagent) -> AtifObservationResult:
    ref = SubagentTrajectoryRef(
        trajectory_id=subagent.trajectory.trajectory_id,
        extra={"subagent_kind": subagent.subagent_kind},
    )
    return AtifObservationResult(
        source_call_id=result.source_call_id,
        # The textual result stays as the quick-reference summary next to the ref.
        content=result.content,
        subagent_trajectory_ref=[*(result.subagent_trajectory_ref or []), ref],
        extra=result.extra,
    )


@pure
def _pending_subagent_result(call_id: str, trajectory_id: str, subagent_kind: str) -> AtifObservationResult:
    # The delegating tool call exists but its observation has not been appended yet
    # (the subagent is still running, or its result line is still in flight). The
    # resolved trajectory is still worth embedding, under a marked placeholder result.
    return AtifObservationResult(
        source_call_id=call_id,
        content=None,
        subagent_trajectory_ref=[
            SubagentTrajectoryRef(trajectory_id=trajectory_id, extra={"subagent_kind": subagent_kind})
        ],
        extra={"subagent_result_pending": True},
    )


@pure
def _require_trajectory_id(subagent: EmbeddedSubagent) -> str:
    trajectory_id = subagent.trajectory.trajectory_id
    if trajectory_id is None:
        raise TrajectoryBuildError("Embedded subagent trajectories must carry a trajectory_id")
    return trajectory_id


@pure
def _with_subagent_kind(trajectory: Trajectory, subagent_kind: str) -> Trajectory:
    merged_extra = {**(trajectory.extra or {}), "subagent_kind": subagent_kind}
    document = trajectory.model_dump()
    document["extra"] = merged_extra
    return Trajectory.model_validate(document)


@pure
def _sum_present(values: Iterable[_NumberT | None]) -> _NumberT | None:
    """Sum the values that are present, or None when every step omitted the metric."""
    present = [value for value in values if value is not None]
    return sum(present) if present else None


@pure
def _sum_final_metrics(steps: Sequence[AtifStep]) -> FinalMetrics:
    step_metrics = [step.metrics for step in steps if step.metrics is not None]
    return FinalMetrics(
        total_prompt_tokens=_sum_present(m.prompt_tokens for m in step_metrics),
        total_completion_tokens=_sum_present(m.completion_tokens for m in step_metrics),
        total_cached_tokens=_sum_present(m.cached_tokens for m in step_metrics),
        total_cost_usd=_sum_present(m.cost_usd for m in step_metrics),
        total_steps=len(steps),
    )


@pure
def build_trajectory_from_records(
    records: Sequence[CommonTranscriptRecord],
    enrichment: TrajectoryEnrichment,
    # Resolved subagents keyed by the tool_call_id of the delegating tool call; results
    # for those calls get a subagent_trajectory_ref and the trajectories are embedded.
    subagent_by_call_id: Mapping[str, EmbeddedSubagent],
) -> TrajectoryBuildResult:
    """Merge stream records (in file order) into a validated ATIF trajectory.

    Raises :class:`TrajectoryBuildError` for a stream that cannot produce a valid
    document (missing header, failed document validation); recoverable problems
    (unmatched observation results) become warnings.
    """
    warnings: list[str] = []
    header = _require_header(records)

    # Walk the stream in file order (append order is authoritative), collecting steps
    # and attaching each observation result to the step whose tool_calls declared its
    # source_call_id.
    accumulators: list[_StepAccumulator] = []
    step_idx_by_call_id: dict[str, int] = {}
    last_agent_step_idx: int | None = None
    for record in records[1:]:
        if isinstance(record, HeaderRecord):
            raise TrajectoryBuildError("Encountered a second header record mid-stream")
        elif isinstance(record, StepRecord):
            step_idx = len(accumulators)
            accumulators.append(_StepAccumulator(record=record, attached_results=()))
            if record.source == StepSource.AGENT:
                last_agent_step_idx = step_idx
            for tool_call in record.tool_calls or ():
                if tool_call.tool_call_id in step_idx_by_call_id:
                    warnings.append(
                        f"Duplicate tool_call_id '{tool_call.tool_call_id}'; results attach to the later step"
                    )
                step_idx_by_call_id[tool_call.tool_call_id] = step_idx
        elif isinstance(record, ObservationRecord):
            for result in record.results:
                matched_step_idx = step_idx_by_call_id.get(result.source_call_id)
                if matched_step_idx is not None:
                    accumulators[matched_step_idx] = accumulators[matched_step_idx].with_result(result)
                elif last_agent_step_idx is not None:
                    warnings.append(
                        f"Observation result for unknown tool call '{result.source_call_id}' preserved on the "
                        "nearest preceding agent step"
                    )
                    accumulators[last_agent_step_idx] = accumulators[last_agent_step_idx].with_result(
                        _redirected_unmatched_result(result)
                    )
                else:
                    warnings.append(
                        f"Dropped observation result for unknown tool call '{result.source_call_id}': "
                        "no preceding agent step to preserve it on"
                    )
        else:
            assert_never(record)

    # Assemble the ATIF steps: sequential step ids, streamed results merged into each
    # step's observation, subagent refs attached, and framing provenance kept in extra.
    steps: list[AtifStep] = []
    embedded_subagent_by_id: dict[str, EmbeddedSubagent] = {}
    for step_idx, accumulator in enumerate(accumulators):
        record = accumulator.record
        final_results: list[AtifObservationResult] = []
        for result in accumulator.attached_results:
            subagent = subagent_by_call_id.get(result.source_call_id) if result.source_call_id is not None else None
            if subagent is None:
                final_results.append(_plain_result(result))
                continue
            embedded_subagent_by_id.setdefault(_require_trajectory_id(subagent), subagent)
            final_results.append(_result_with_subagent_ref(result, subagent))
        # A delegating call that this step declared but whose observation has not
        # arrived yet still gets its resolved subagent embedded, under a synthesized
        # pending result -- otherwise the whole subagent trajectory would be dropped.
        resolved_call_ids = {
            result.source_call_id for result in accumulator.attached_results if result.source_call_id is not None
        }
        for call_id, pending_subagent in subagent_by_call_id.items():
            if step_idx_by_call_id.get(call_id) != step_idx or call_id in resolved_call_ids:
                continue
            pending_trajectory_id = _require_trajectory_id(pending_subagent)
            embedded_subagent_by_id.setdefault(pending_trajectory_id, pending_subagent)
            final_results.append(
                _pending_subagent_result(call_id, pending_trajectory_id, pending_subagent.subagent_kind)
            )
            warnings.append(f"Delegating call '{call_id}' has no result yet; embedded with a pending marker")
        if final_results:
            observation = AtifObservation(results=final_results)
        else:
            observation = record.observation
        merged_extra = {**(record.extra or {}), "event_id": record.event_id, "emitter": record.emitter}
        steps.append(record.to_atif_step(step_id=step_idx + 1, observation=observation, extra=merged_extra))

    for call_id in subagent_by_call_id:
        if call_id not in step_idx_by_call_id:
            warnings.append(f"Resolved subagent for tool call '{call_id}' has no matching tool call in the stream")

    embedded_trajectories = [
        _with_subagent_kind(subagent.trajectory, subagent.subagent_kind)
        for subagent in embedded_subagent_by_id.values()
    ]

    # Build and validate the document; the vendored model enforces the cross-step rules
    # (sequential step ids, observation/tool-call reference integrity, embedded ids).
    try:
        trajectory = Trajectory(
            schema_version=header.schema_version,
            session_id=enrichment.session_id,
            trajectory_id=enrichment.trajectory_id,
            agent=AtifAgent(name=enrichment.agent_name, version=enrichment.agent_version),
            steps=steps,
            final_metrics=_sum_final_metrics(steps),
            subagent_trajectories=embedded_trajectories if embedded_trajectories else None,
        )
    except pydantic.ValidationError as e:
        raise TrajectoryBuildError(f"Assembled document failed ATIF validation: {e}") from e
    return TrajectoryBuildResult(trajectory=trajectory, warnings=tuple(warnings))
