"""Canonical schema for the agent-agnostic common-transcript stream.

Every agent plugin emits a common, agent-type-independent JSONL stream (claude,
codex, antigravity, opencode, pi-coding). Each line is one record whose ``type``
is ``header``, ``step``, or ``observation``: a streaming form of ATIF (Harbor's
Agent Trajectory Interchange Format; see ``specs/atif-transcript-alignment/spec.md``
and the vendored models under :mod:`imbue.mngr.agents.data_types.atif`). Streams
written before the ATIF cutover (see :data:`LEGACY_RECORD_TYPE_NAMES`) are reported
as unsupported rather than rendered or rebuilt.

Each record carries three *framing fields* that belong to mngr's stream container,
not to ATIF: ``type`` (the record discriminator), ``event_id`` (the source-derived
idempotency key emitters dedup on), and ``emitter`` (the emitting source, e.g.
``claude/common_transcript``). All remaining fields are ATIF fields, composed from
the vendored sub-models so the payload shapes are stated exactly once. The
doc-builder strips the framing fields when assembling a full ATIF document.

This module is the single source of truth for that contract. The contract is
enforced at *emit* time, not read time: each plugin's conformance test asserts its
emitter's real output validates against this schema, so the five independently
written emitters (opencode and pi-coding in TypeScript; claude, antigravity, and
codex in shell+Python) cannot silently drift on the shared fields. The reader
(:mod:`imbue.mngr.cli.transcript`) deliberately stays tolerant -- it renders
whatever an agent emitted rather than validating against this schema.

The records are strict (``extra="forbid"``): per-agent annotations belong under
the ATIF ``extra`` objects, so the doc-builder can treat every other field as an
ATIF field.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import auto
from typing import Annotated
from typing import Any
from typing import Final
from typing import Literal
from typing import assert_never
from typing import get_args

import pydantic
from pydantic import AfterValidator
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.agents.data_types.atif.metrics import Metrics as AtifMetrics
from imbue.mngr.agents.data_types.atif.observation import Observation as AtifObservation
from imbue.mngr.agents.data_types.atif.observation_result import ObservationResult as AtifObservationResult
from imbue.mngr.agents.data_types.atif.step import Step as AtifStep
from imbue.mngr.agents.data_types.atif.tool_call import ToolCall as AtifToolCall
from imbue.mngr.errors import InvalidCommonTranscriptRecordError

# The ATIF revision the stream records follow. Bumping it is a deliberate act that
# goes with re-vendoring imbue/mngr/agents/atif/ (see the README there).
AtifSchemaVersion = Literal["ATIF-v1.7"]
PINNED_ATIF_SCHEMA_VERSION: Final[str] = get_args(AtifSchemaVersion)[0]

# The step_id used when validating a stream step against the vendored ATIF Step model.
# Real step ids are assigned sequentially by the doc-builder; any valid value works for
# per-record validation because step-id sequencing is a document-level (Trajectory) rule.
_VALIDATION_PLACEHOLDER_STEP_ID = 1


def _require_iso_timestamp(value: str) -> str:
    # Mirrors the vendored ATIF Step timestamp check so observation records, which the
    # vendored models never see, are held to the same standard as step records.
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidCommonTranscriptRecordError(f"invalid ISO 8601 timestamp: {e}") from e
    return value


IsoTimestamp = Annotated[str, AfterValidator(_require_iso_timestamp)]


def _format_validation_error(error: pydantic.ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in error.errors())
    return problems or str(error)


class StepSource(LowerCaseStrEnum):
    """The ATIF step originator."""

    SYSTEM = auto()
    USER = auto()
    AGENT = auto()


class _AtifRecordModel(FrozenModel):
    """Base for the ATIF-shaped stream records: immutable and strict.

    ``extra="forbid"`` because every non-framing field must be an ATIF field for the
    doc-builder to assemble documents mechanically; per-agent annotations go under
    the ATIF ``extra`` objects instead. Only the record itself is frozen: the nested
    vendored ATIF sub-models are plain mutable ``BaseModel``s.
    """


class HeaderRecord(_AtifRecordModel):
    """The first line of every stream, pinning the ATIF revision its records follow.

    The header id hashes the agent id and emitter (``header-<sha256 hex>``) so
    analytics' fleet-wide event-id dedupe never collapses different agents' headers.
    """

    type: Literal["header"]
    event_id: str = Field(pattern=r"^header-[0-9a-f]{32}$")
    emitter: str
    schema_version: AtifSchemaVersion


class StepRecord(_AtifRecordModel):
    """One ATIF step: a user message, one assistant inference, or a system event.

    Carries the ATIF ``StepObject`` fields except ``step_id`` (assigned at build
    time). Agent steps stream their tool results separately as
    :class:`ObservationRecord` lines (they arrive after the inference); system
    steps that already have their result at emission time carry ``observation``
    inline. ATIF has no stop-reason field, so emitters record ``finish_reason``
    under ``extra``.
    """

    type: Literal["step"]
    event_id: str
    emitter: str
    # Required in the stream (unlike ATIF, where it is optional): it is the step
    # timestamp in the built document and doubles as the stream ordering aid.
    timestamp: IsoTimestamp
    # The member values are the ATIF wire vocabulary for the step originator.
    source: StepSource
    message: str
    model_name: str | None = None
    reasoning_effort: str | float | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[AtifToolCall, ...] | None = None
    observation: AtifObservation | None = None
    metrics: AtifMetrics | None = None
    llm_call_count: int | None = None
    is_copied_context: bool | None = None
    extra: Mapping[str, Any] | None = None

    def to_atif_step(
        self,
        step_id: int,
        # The observation to place on the built step: the record's own inline observation
        # (system steps), or one the doc-builder assembled from streamed observation records
        # (agent steps).
        observation: AtifObservation | None,
        # The extra mapping to place on the built step (the doc-builder merges the record's
        # extra with stream-framing provenance).
        extra: Mapping[str, Any] | None,
    ) -> AtifStep:
        """Build the vendored ATIF Step this record represents (used by the doc-builder).

        The vendored sub-models (tool calls, metrics, the passed observation) are shared
        with their sources rather than copied, so callers must not mutate the returned
        step's sub-models.
        """
        match self.source:
            case StepSource.SYSTEM:
                atif_source = "system"
            case StepSource.USER:
                atif_source = "user"
            case StepSource.AGENT:
                atif_source = "agent"
            case _ as unreachable:
                assert_never(unreachable)
        return AtifStep(
            step_id=step_id,
            timestamp=self.timestamp,
            source=atif_source,
            message=self.message,
            model_name=self.model_name,
            reasoning_effort=self.reasoning_effort,
            reasoning_content=self.reasoning_content,
            tool_calls=list(self.tool_calls) if self.tool_calls is not None else None,
            observation=observation,
            metrics=self.metrics,
            llm_call_count=self.llm_call_count,
            is_copied_context=self.is_copied_context,
            extra=dict(extra) if extra is not None else None,
        )

    @model_validator(mode="after")
    def _validate_as_atif_step(self) -> "StepRecord":
        # Reuse the vendored Step validators (ISO timestamp, agent-only fields,
        # llm_call_count=0 rules) so emit-time conformance implies build-time validity.
        try:
            self.to_atif_step(step_id=_VALIDATION_PLACEHOLDER_STEP_ID, observation=self.observation, extra=self.extra)
        except pydantic.ValidationError as error:
            raise InvalidCommonTranscriptRecordError(
                f"step record is not a valid ATIF step: {_format_validation_error(error)}"
            ) from error
        return self

    @model_validator(mode="after")
    def _validate_inline_observation_is_system_only(self) -> "StepRecord":
        # Agent-step results are streamed as separate observation records (there is an
        # async gap to bridge); only system steps carry their result inline.
        if self.observation is not None and self.source != StepSource.SYSTEM:
            raise InvalidCommonTranscriptRecordError(
                "only system steps may carry an inline observation; agent tool results are streamed as observation records"
            )
        return self


class StreamObservationResult(AtifObservationResult):
    """An ATIF observation result as it appears in the stream.

    The call id is required; call-id-less results ride inline on their system step.
    """

    source_call_id: str


class ObservationRecord(_AtifRecordModel):
    """Tool results for a previously streamed agent step, as they arrive.

    Each result is an ATIF ``ObservationResultSchema`` object and must carry
    ``source_call_id`` so the doc-builder can attach it to the step whose
    ``tool_calls`` contains that id (call-id-less system results ride inline on
    their system step instead).
    """

    type: Literal["observation"]
    event_id: str
    emitter: str
    timestamp: IsoTimestamp
    results: tuple[StreamObservationResult, ...] = Field(min_length=1)


# The ``type`` discriminators of the retired pre-ATIF records. They are no longer part of
# the schema; this is the permanent detection list that lets the reader and the doc-builder
# report a pre-cutover agent's stream as unsupported with a clear error instead of a generic
# schema violation. Their presence anywhere in a stream marks the whole stream as old-format:
# streams never mix formats, since an agent keeps the emitter it was provisioned with.
LEGACY_RECORD_TYPE_NAMES: Final[frozenset[str]] = frozenset({"user_message", "assistant_message", "tool_result"})

# The shared tail of every old-format error, so the reader and the doc-builder explain the
# same situation the same way; each site prefixes its own context.
OLD_FORMAT_STREAM_EXPLANATION: Final[str] = (
    "an agent keeps the emitter it was provisioned with, so this agent predates the ATIF "
    "cutover, and old-format streams are not supported."
)


CommonTranscriptRecord = Annotated[
    HeaderRecord | StepRecord | ObservationRecord,
    Field(discriminator="type"),
]

_RECORD_ADAPTER: pydantic.TypeAdapter[CommonTranscriptRecord] = pydantic.TypeAdapter(CommonTranscriptRecord)


def parse_common_transcript_record(data: Mapping[str, Any]) -> CommonTranscriptRecord:
    """Validate ``data`` against the canonical schema and return the typed record.

    Raises :class:`InvalidCommonTranscriptRecordError` if it does not conform. Use
    this when you want the typed record (e.g. to assert on fields in a conformance
    test); use :func:`validate_common_transcript_record` for the non-raising form.
    """
    try:
        return _RECORD_ADAPTER.validate_python(data)
    except pydantic.ValidationError as error:
        raise InvalidCommonTranscriptRecordError(_format_validation_error(error)) from error


def validate_common_transcript_record(data: Mapping[str, Any]) -> str | None:
    """Return ``None`` if ``data`` conforms to the canonical schema, else a short error.

    Non-raising counterpart to :func:`parse_common_transcript_record`, for callers
    (like the transcript reader) that want to surface drift without failing.
    """
    try:
        _RECORD_ADAPTER.validate_python(data)
    except pydantic.ValidationError as error:
        return _format_validation_error(error)
    return None
