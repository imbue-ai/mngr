"""Canonical schema for the agent-agnostic common-transcript envelope.

``mngr transcript`` reads a common, agent-type-independent JSONL stream that
every agent plugin emits (claude, antigravity, opencode, pi-coding, ...). Each
line is one record whose ``type`` is ``user_message``, ``assistant_message``, or
``tool_result``, carrying the shared envelope fields (``timestamp``,
``event_id``, ``source``) plus a per-type payload.

This module is the single source of truth for that contract. The contract is
enforced at *emit* time, not read time: each plugin's conformance test asserts its
emitter's real output validates against this schema, so the five independently
written emitters (opencode and pi-coding in TypeScript; claude, antigravity, and
codex in shell+Python) cannot silently drift on the shared fields. The reader
(:mod:`imbue.mngr.cli.transcript`) deliberately stays tolerant -- it renders
whatever an agent emitted rather than validating against this schema -- so a
slightly-off line is still shown rather than dropped.

The schema is deliberately strict on the core contract every record and
record-type must satisfy, but permissive on *optional* fields that legitimately
vary by agent: a CLI that exposes token usage populates ``usage`` while one that
does not leaves it ``null``/absent; ``model`` may be unknown. Every assistant record
carries an ordered ``parts`` array (text/tool_call segments, modelled on the
OpenTelemetry GenAI message parts) -- the canonical, agent-agnostic view of the turn
that readers render -- with ``parts_ordered=False`` marking the one case where the
order is best-effort rather than faithful (antigravity). Unknown extra fields are
allowed, so a plugin annotating its records with its own field (e.g. antigravity's and
opencode's ``conversation_id``, opencode's ``message_id``) is forward-compatible. Adding a *new record type*, by contrast, means adding it
here -- that is the point of a single source of truth.
"""

from collections.abc import Mapping
from typing import Annotated
from typing import Any
from typing import Literal

import pydantic
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class _RecordModel(BaseModel):
    """Base for the envelope records: immutable, but tolerant of per-agent extras.

    ``frozen`` matches the repo's :class:`FrozenModel` convention; ``extra="allow"``
    (rather than ``forbid``) is required because different plugins annotate records
    with their own optional fields, and the reader must tolerate them rather than
    reject a whole line.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


class ToolCall(_RecordModel):
    """One tool invocation inside an :class:`AssistantMessageRecord`."""

    tool_call_id: str
    tool_name: str
    input_preview: str


class TextPart(_RecordModel):
    """An ordered text segment of an assistant turn (see :class:`AssistantMessageRecord.parts`)."""

    type: Literal["text"]
    content: str


class ToolCallPart(_RecordModel):
    """An ordered tool invocation of an assistant turn (see :class:`AssistantMessageRecord.parts`).

    Field names match :class:`ToolCall` (the flat ``tool_calls`` list) so the record carries one
    naming scheme. ``input_preview`` is a truncated preview, not the full arguments payload.
    """

    type: Literal["tool_call"]
    tool_call_id: str
    tool_name: str
    input_preview: str


# An ordered assistant-turn segment, modelled on the OpenTelemetry GenAI message ``parts``.
AssistantPart = Annotated[TextPart | ToolCallPart, Field(discriminator="type")]


class UserMessageRecord(_RecordModel):
    type: Literal["user_message"]
    timestamp: str
    event_id: str
    source: str
    role: Literal["user"] = "user"
    content: str


class AssistantMessageRecord(_RecordModel):
    type: Literal["assistant_message"]
    timestamp: str
    event_id: str
    source: str
    role: Literal["assistant"] = "assistant"
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    # ``parts`` is the canonical ordered view of the turn (text/tool_call segments, modelled
    # on the OTel GenAI message parts) and is filled by *every* emitter -- it is what readers
    # render. The flat ``text``/``tool_calls`` are kept as a convenience baseline (a record
    # carries both; ``parts`` is authoritative for ordering). ``parts_ordered`` is False when
    # the emitter could only synthesize a best-effort order because its native format does not
    # record where tool calls sat relative to the text (antigravity); it is True when the order
    # is faithful (claude, pi-coding, opencode) or trivial (codex, whose assistant turns are
    # either text-only or a single tool_call, each its own rollout item).
    # ``model`` may be unknown ("" or null); ``usage`` is populated only by CLIs that expose
    # token counts; ``finish_reason`` (the OTel GenAI term for the stop reason) is absent on
    # agents that do not report one (e.g. pi-coding).
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    finish_reason: str | None = None
    parts: tuple[AssistantPart, ...] = ()
    parts_ordered: bool = True


class ToolResultRecord(_RecordModel):
    type: Literal["tool_result"]
    timestamp: str
    event_id: str
    source: str
    # tool_result carries no explicit role; the reader derives "tool" from the type.
    tool_call_id: str
    tool_name: str
    output: str
    is_error: bool


CommonTranscriptRecord = Annotated[
    UserMessageRecord | AssistantMessageRecord | ToolResultRecord,
    Field(discriminator="type"),
]

_RECORD_ADAPTER: pydantic.TypeAdapter[CommonTranscriptRecord] = pydantic.TypeAdapter(CommonTranscriptRecord)


def parse_common_transcript_record(data: Mapping[str, Any]) -> CommonTranscriptRecord:
    """Validate ``data`` against the canonical schema and return the typed record.

    Raises :class:`pydantic.ValidationError` if it does not conform. Use this when
    you want the typed record (e.g. to assert on fields in a conformance test);
    use :func:`validate_common_transcript_record` for the non-raising form.
    """
    return _RECORD_ADAPTER.validate_python(data)


def validate_common_transcript_record(data: Mapping[str, Any]) -> str | None:
    """Return ``None`` if ``data`` conforms to the canonical schema, else a short error.

    Non-raising counterpart to :func:`parse_common_transcript_record`, for callers
    (like the transcript reader) that want to surface drift without failing.
    """
    try:
        _RECORD_ADAPTER.validate_python(data)
    except pydantic.ValidationError as error:
        problems = "; ".join(f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in error.errors())
        return problems or str(error)
    return None
