"""Unit tests for the canonical common-transcript record schema.

The golden records below mirror the *real* output shapes of all five emitters
(claude, antigravity, opencode, pi-coding, codex), captured from their resource
scripts. They are the executable statement of "these five independently written
emitters agree on the shared contract" -- if a schema change rejected any of them,
that would be a regression in the contract, not the emitter.
"""

from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import HeaderRecord
from imbue.mngr.agents.common_transcript_records import LEGACY_RECORD_TYPE_NAMES
from imbue.mngr.agents.common_transcript_records import ObservationRecord
from imbue.mngr.agents.common_transcript_records import StepRecord
from imbue.mngr.agents.common_transcript_records import parse_common_transcript_record
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr.agents.data_types.atif.step import Step as AtifStep
from imbue.mngr.errors import InvalidCommonTranscriptRecordError

# Golden records (header / step / observation) mirroring what the emitters produce.
# The shapes are agent-agnostic by design; the variations that matter are which
# optional fields a given CLI can populate (reasoning, metrics, finish_reason under
# extra).
_VALID_ATIF_RECORDS: dict[str, dict[str, Any]] = {
    "header": {
        "type": "header",
        "event_id": "header-" + "0" * 32,
        "emitter": "claude/common_transcript",
        "schema_version": "ATIF-v1.7",
    },
    "user_step": {
        "type": "step",
        "event_id": "uuid-1-user",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-06-09T12:00:00Z",
        "source": "user",
        "message": "do the thing",
    },
    # A full agent turn: text, thinking, complete tool-call arguments, ATIF metric
    # names with provider extras, and the stop reason under extra.
    "agent_step": {
        "type": "step",
        "event_id": "uuid-2-assistant",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-06-09T12:00:01Z",
        "source": "agent",
        "message": "running it now",
        "reasoning_content": "the user wants the thing done",
        "model_name": "claude-haiku-4-5",
        "tool_calls": [
            {
                "tool_call_id": "toolu_abc",
                "function_name": "Bash",
                "arguments": {"command": "echo hi", "timeout": 120000},
            }
        ],
        "metrics": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 50,
            "extra": {"cache_creation_input_tokens": 10},
        },
        "extra": {"finish_reason": "tool_use", "message_uuid": "uuid-2"},
    },
    # Every StepRecord field an agent step may carry, pinning the full field mapping
    # into the vendored ATIF Step (an inline observation is system-only, so absent).
    "full_agent_step": {
        "type": "step",
        "event_id": "uuid-9-assistant",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-06-09T12:00:05Z",
        "source": "agent",
        "message": "running it now",
        "model_name": "claude-haiku-4-5",
        "reasoning_effort": "high",
        "reasoning_content": "the user wants the thing done",
        "tool_calls": [
            {
                "tool_call_id": "toolu_xyz",
                "function_name": "Bash",
                "arguments": {"command": "echo hi"},
                "extra": {"tool_version": "1"},
            }
        ],
        "metrics": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 50,
            "cost_usd": 0.01,
            "extra": {"cache_creation_input_tokens": 10},
        },
        "llm_call_count": 1,
        "is_copied_context": False,
        "extra": {"finish_reason": "tool_use"},
    },
    # A minimal agent turn from a CLI that exposes no model/metrics (codex, antigravity).
    "bare_agent_step": {
        "type": "step",
        "event_id": "line-7-assistant",
        "emitter": "codex/common_transcript",
        "timestamp": "2026-06-09T12:00:02Z",
        "source": "agent",
        "message": "done",
    },
    # A compaction event: a system step whose result is known at emission time, so the
    # observation rides inline, with the ATIF v1.7 context-management convention in extra.
    "system_compaction_step": {
        "type": "step",
        "event_id": "uuid-3-system",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-06-09T12:00:03Z",
        "source": "system",
        "message": "Context compaction performed",
        "observation": {"results": [{"content": "Summary: prior conversation covered the thing."}]},
        "extra": {"context_management": {"type": "compaction", "boundary": "replace"}},
    },
    "observation": {
        "type": "observation",
        "event_id": "uuid-2-tool_result-toolu_abc",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-06-09T12:00:04Z",
        "results": [
            {
                "source_call_id": "toolu_abc",
                "content": "hi",
                "extra": {"is_error": False, "tool_name": "Bash"},
            }
        ],
    },
}


@pytest.mark.parametrize("name", sorted(_VALID_ATIF_RECORDS))
def test_atif_record_shapes_validate(name: str) -> None:
    record = _VALID_ATIF_RECORDS[name]
    assert validate_common_transcript_record(record) is None, f"{name} should conform"
    parsed = parse_common_transcript_record(record)
    assert parsed.type == record["type"]


def test_atif_parsed_types_match_expected_classes() -> None:
    assert isinstance(parse_common_transcript_record(_VALID_ATIF_RECORDS["header"]), HeaderRecord)
    assert isinstance(parse_common_transcript_record(_VALID_ATIF_RECORDS["agent_step"]), StepRecord)
    assert isinstance(parse_common_transcript_record(_VALID_ATIF_RECORDS["observation"]), ObservationRecord)


def test_step_record_converts_to_valid_atif_step_with_assigned_id() -> None:
    parsed = parse_common_transcript_record(_VALID_ATIF_RECORDS["agent_step"])
    assert isinstance(parsed, StepRecord)

    atif_step = parsed.to_atif_step(step_id=7, observation=parsed.observation, extra=parsed.extra)

    assert atif_step.step_id == 7
    assert atif_step.source == "agent"
    assert atif_step.tool_calls is not None
    assert atif_step.tool_calls[0].arguments == {"command": "echo hi", "timeout": 120000}
    assert atif_step.extra is not None and atif_step.extra["finish_reason"] == "tool_use"


def test_header_with_unpinned_schema_version_is_rejected() -> None:
    record = dict(_VALID_ATIF_RECORDS["header"])
    record["schema_version"] = "ATIF-v1.6"
    error = validate_common_transcript_record(record)
    assert error is not None and "schema_version" in error


def test_step_record_rejects_unknown_top_level_fields() -> None:
    # ATIF-shaped records are strict: per-agent annotations belong under extra, so the
    # doc-builder can treat every non-framing field as an ATIF field.
    record = dict(_VALID_ATIF_RECORDS["agent_step"])
    record["message_uuid"] = "uuid-2"
    error = validate_common_transcript_record(record)
    assert error is not None and "message_uuid" in error


def test_step_record_requires_timestamp() -> None:
    record = dict(_VALID_ATIF_RECORDS["user_step"])
    del record["timestamp"]
    error = validate_common_transcript_record(record)
    assert error is not None and "timestamp" in error


@pytest.mark.parametrize(
    ("record_name", "field_name", "bad_value"),
    (
        ("user_step", "timestamp", "yesterday-ish"),
        ("user_step", "reasoning_content", "thinking"),
        ("agent_step", "llm_call_count", 0),
    ),
)
def test_step_record_inherits_the_vendored_atif_step_validators(
    record_name: str, field_name: str, bad_value: object
) -> None:
    # The record delegates to the vendored Step validators, so their rules (ISO
    # timestamps, agent-only fields, the llm_call_count=0 dispatch rule) apply at emit time.
    record = dict(_VALID_ATIF_RECORDS[record_name])
    record[field_name] = bad_value
    error = validate_common_transcript_record(record)
    assert error is not None and field_name in error.lower()


def test_step_record_rejects_inline_observation_on_agent_steps() -> None:
    record = dict(_VALID_ATIF_RECORDS["agent_step"])
    record["observation"] = {"results": [{"source_call_id": "toolu_abc", "content": "hi"}]}
    error = validate_common_transcript_record(record)
    assert error is not None and "system steps" in error


def test_tool_call_arguments_must_be_an_object() -> None:
    record = dict(_VALID_ATIF_RECORDS["agent_step"])
    record["tool_calls"] = [{"tool_call_id": "t", "function_name": "Bash", "arguments": "echo hi"}]
    error = validate_common_transcript_record(record)
    assert error is not None and "arguments" in error


def test_observation_record_requires_source_call_ids() -> None:
    record = dict(_VALID_ATIF_RECORDS["observation"])
    record["results"] = [{"content": "orphan output"}]
    error = validate_common_transcript_record(record)
    assert error is not None and "source_call_id" in error


def test_full_agent_step_maps_every_field_onto_the_atif_step() -> None:
    parsed = parse_common_transcript_record(_VALID_ATIF_RECORDS["full_agent_step"])
    assert isinstance(parsed, StepRecord)

    assert parsed.to_atif_step(step_id=7, observation=parsed.observation, extra=parsed.extra).model_dump() == {
        "step_id": 7,
        "timestamp": "2026-06-09T12:00:05Z",
        "source": "agent",
        "model_name": "claude-haiku-4-5",
        "reasoning_effort": "high",
        "message": "running it now",
        "reasoning_content": "the user wants the thing done",
        "tool_calls": [
            {
                "tool_call_id": "toolu_xyz",
                "function_name": "Bash",
                "arguments": {"command": "echo hi"},
                "extra": {"tool_version": "1"},
            }
        ],
        "observation": None,
        "metrics": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 50,
            "cost_usd": 0.01,
            "prompt_token_ids": None,
            "completion_token_ids": None,
            "logprobs": None,
            "extra": {"cache_creation_input_tokens": 10},
        },
        "is_copied_context": False,
        "llm_call_count": 1,
        "extra": {"finish_reason": "tool_use"},
    }


def test_system_step_with_inline_observation_round_trips_to_a_valid_atif_step() -> None:
    parsed = parse_common_transcript_record(_VALID_ATIF_RECORDS["system_compaction_step"])
    assert isinstance(parsed, StepRecord)

    rebuilt = AtifStep.model_validate(
        parsed.to_atif_step(step_id=3, observation=parsed.observation, extra=parsed.extra).model_dump()
    )

    assert rebuilt.source == "system"
    assert rebuilt.observation is not None
    assert rebuilt.observation.results[0].content == "Summary: prior conversation covered the thing."
    assert rebuilt.observation.results[0].source_call_id is None


def test_step_record_requires_emitter() -> None:
    record = dict(_VALID_ATIF_RECORDS["user_step"])
    del record["emitter"]
    error = validate_common_transcript_record(record)
    assert error is not None and "emitter" in error


def test_observation_record_requires_event_id() -> None:
    record = dict(_VALID_ATIF_RECORDS["observation"])
    del record["event_id"]
    error = validate_common_transcript_record(record)
    assert error is not None and "event_id" in error


def test_header_record_requires_the_header_event_id() -> None:
    record = dict(_VALID_ATIF_RECORDS["header"])
    record["event_id"] = "header-1"
    error = validate_common_transcript_record(record)
    assert error is not None and "event_id" in error


def test_observation_record_rejects_malformed_timestamp() -> None:
    record = dict(_VALID_ATIF_RECORDS["observation"])
    record["timestamp"] = "yesterday-ish"
    error = validate_common_transcript_record(record)
    assert error is not None and "timestamp" in error.lower()


def test_observation_record_rejects_empty_results() -> None:
    record = dict(_VALID_ATIF_RECORDS["observation"])
    record["results"] = []
    error = validate_common_transcript_record(record)
    assert error is not None and "results" in error


def test_parse_raises_the_domain_error_for_an_invalid_record() -> None:
    record = dict(_VALID_ATIF_RECORDS["user_step"])
    del record["message"]
    with pytest.raises(InvalidCommonTranscriptRecordError) as exc_info:
        parse_common_transcript_record(record)
    assert "message" in str(exc_info.value)


def test_unknown_record_type_is_rejected() -> None:
    # An unrecognised `type` means the emitter introduced a record type the shared
    # schema does not know -- surface it rather than silently accept.
    error = validate_common_transcript_record({"type": "thinking", "timestamp": "t", "event_id": "e", "emitter": "s"})
    assert error is not None


@pytest.mark.parametrize("legacy_type", sorted(LEGACY_RECORD_TYPE_NAMES))
def test_retired_pre_atif_records_no_longer_validate(legacy_type: str) -> None:
    # Detection of these lives in LEGACY_RECORD_TYPE_NAMES; the schema simply rejects them.
    error = validate_common_transcript_record(
        {"type": legacy_type, "timestamp": "2026-06-09T12:00:00Z", "event_id": "e1", "source": "claude", "text": "hi"}
    )
    assert error is not None and legacy_type in error
