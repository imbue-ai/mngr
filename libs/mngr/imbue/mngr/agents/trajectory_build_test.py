"""Unit tests for the pure ATIF doc-builder merge logic."""

import json
from typing import Any

import pytest
from inline_snapshot import snapshot

from imbue.mngr.agents.common_transcript_records import CommonTranscriptRecord
from imbue.mngr.agents.data_types.atif.trajectory import Trajectory
from imbue.mngr.agents.trajectory_build import EmbeddedSubagent
from imbue.mngr.agents.trajectory_build import MNGR_SUBAGENT_KIND
from imbue.mngr.agents.trajectory_build import TrajectoryEnrichment
from imbue.mngr.agents.trajectory_build import build_trajectory_from_records
from imbue.mngr.agents.trajectory_build import parse_stream_content
from imbue.mngr.errors import TrajectoryBuildError
from imbue.mngr.utils.testing import capture_loguru

_EMITTER = "claude/common_transcript"


def _make_enrichment() -> TrajectoryEnrichment:
    return TrajectoryEnrichment(
        agent_name="claude",
        agent_version="unknown",
        session_id="agent-1234",
        trajectory_id="agent-1234",
    )


def _header() -> dict[str, Any]:
    return {"type": "header", "event_id": "header-" + "0" * 32, "emitter": _EMITTER, "schema_version": "ATIF-v1.7"}


def _user_step(event_id: str, timestamp: str, message: str) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": "user",
        "message": message,
    }


def _agent_step_with_tool_calls(event_id: str, timestamp: str, call_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": "agent",
        "message": "running",
        "model_name": "claude-haiku-4-5",
        "tool_calls": [
            {"tool_call_id": call_id, "function_name": "Bash", "arguments": {"command": f"echo {call_id}"}}
            for call_id in call_ids
        ],
        "metrics": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 40},
        "extra": {"finish_reason": "tool_use"},
    }


def _observation(event_id: str, timestamp: str, call_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "observation",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "results": [
            {"source_call_id": call_id, "content": content, "extra": {"is_error": False, "tool_name": "Bash"}}
        ],
    }


def _parse(raw_records: list[dict[str, Any]]) -> tuple[CommonTranscriptRecord, ...]:
    content = "\n".join(json.dumps(r) for r in raw_records) + "\n"
    return parse_stream_content(content, source_description="test stream")


def test_full_stream_builds_a_validated_trajectory() -> None:
    records = _parse(
        [
            _header(),
            _user_step("u1-user", "2026-06-09T12:00:00Z", "do the thing"),
            _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["call_a", "call_b"]),
            _observation("o1", "2026-06-09T12:00:02Z", "call_a", "out a"),
            _observation("o2", "2026-06-09T12:00:03Z", "call_b", "out b"),
            _user_step("u2-user", "2026-06-09T12:00:04Z", "thanks"),
        ]
    )

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    assert result.warnings == ()
    trajectory = result.trajectory
    # Round-trips through the vendored model (constructor already validated).
    assert Trajectory.model_validate(trajectory.to_json_dict()) == trajectory
    assert [step.step_id for step in trajectory.steps] == [1, 2, 3]
    agent_step = trajectory.steps[1]
    assert agent_step.observation is not None
    assert [result.source_call_id for result in agent_step.observation.results] == ["call_a", "call_b"]
    assert trajectory.session_id == "agent-1234"
    assert trajectory.agent.name == "claude"
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 100
    assert trajectory.final_metrics.total_steps == 3
    # Framing provenance survives under step.extra.
    assert agent_step.extra is not None
    assert agent_step.extra["event_id"] == "a1-assistant"
    assert agent_step.extra["emitter"] == _EMITTER
    assert agent_step.extra["finish_reason"] == "tool_use"


def test_built_document_golden_shape() -> None:
    records = _parse(
        [
            _header(),
            _user_step("u1-user", "2026-06-09T12:00:00Z", "hi"),
            _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["call_a"]),
            _observation("o1", "2026-06-09T12:00:02Z", "call_a", "out a"),
        ]
    )

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    assert result.trajectory.to_json_dict() == snapshot(
        {
            "schema_version": "ATIF-v1.7",
            "session_id": "agent-1234",
            "trajectory_id": "agent-1234",
            "agent": {"name": "claude", "version": "unknown"},
            "steps": [
                {
                    "step_id": 1,
                    "timestamp": "2026-06-09T12:00:00Z",
                    "source": "user",
                    "message": "hi",
                    "extra": {"event_id": "u1-user", "emitter": "claude/common_transcript"},
                },
                {
                    "step_id": 2,
                    "timestamp": "2026-06-09T12:00:01Z",
                    "source": "agent",
                    "model_name": "claude-haiku-4-5",
                    "message": "running",
                    "tool_calls": [
                        {
                            "tool_call_id": "call_a",
                            "function_name": "Bash",
                            "arguments": {"command": "echo call_a"},
                        }
                    ],
                    "observation": {
                        "results": [
                            {
                                "source_call_id": "call_a",
                                "content": "out a",
                                "extra": {"is_error": False, "tool_name": "Bash"},
                            }
                        ]
                    },
                    "metrics": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 40},
                    "extra": {
                        "finish_reason": "tool_use",
                        "event_id": "a1-assistant",
                        "emitter": "claude/common_transcript",
                    },
                },
            ],
            "final_metrics": {
                "total_prompt_tokens": 100,
                "total_completion_tokens": 10,
                "total_cached_tokens": 40,
                "total_steps": 2,
            },
        }
    )


def test_system_step_keeps_its_inline_observation() -> None:
    compaction_step = {
        "type": "step",
        "event_id": "s1-system",
        "emitter": _EMITTER,
        "timestamp": "2026-06-09T12:00:05Z",
        "source": "system",
        "message": "Context compaction performed",
        "observation": {"results": [{"content": "Summary: prior context."}]},
        "extra": {"context_management": {"type": "compaction", "boundary": "replace"}},
    }
    records = _parse([_header(), _user_step("u1-user", "2026-06-09T12:00:00Z", "hi"), compaction_step])

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    system_step = result.trajectory.steps[1]
    assert system_step.source == "system"
    assert system_step.observation is not None
    assert system_step.observation.results[0].content == "Summary: prior context."
    assert system_step.extra is not None
    assert system_step.extra["context_management"] == {"type": "compaction", "boundary": "replace"}


def test_empty_stream_is_a_build_error() -> None:
    with pytest.raises(TrajectoryBuildError, match="empty stream"):
        build_trajectory_from_records((), _make_enrichment(), {})


def test_stream_not_starting_with_header_is_a_build_error() -> None:
    records = _parse([_user_step("u1-user", "2026-06-09T12:00:00Z", "hi")])

    with pytest.raises(TrajectoryBuildError, match="pre-ATIF emitter"):
        build_trajectory_from_records(records, _make_enrichment(), {})


def test_legacy_stream_is_reported_as_pre_atif_at_parse_time() -> None:
    legacy_record = {
        "type": "user_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "e1",
        "source": _EMITTER,
        "role": "user",
        "content": "Hello",
    }

    with pytest.raises(TrajectoryBuildError, match="pre-ATIF emitter"):
        _parse([legacy_record])


def test_second_header_mid_stream_is_a_build_error() -> None:
    records = _parse([_header(), _user_step("u1-user", "2026-06-09T12:00:00Z", "hi"), _header()])

    with pytest.raises(TrajectoryBuildError, match="second header"):
        build_trajectory_from_records(records, _make_enrichment(), {})


def test_header_only_stream_fails_document_validation() -> None:
    records = _parse([_header()])

    with pytest.raises(TrajectoryBuildError, match="ATIF validation"):
        build_trajectory_from_records(records, _make_enrichment(), {})


def test_malformed_json_line_is_skipped_with_logged_warning() -> None:
    content = (
        json.dumps(_header())
        + "\n"
        + '{"type": "step", "truncated'
        + "\n"
        + json.dumps(_user_step("u1-user", "2026-06-09T12:00:00Z", "hi"))
    )

    with capture_loguru() as captured:
        records = parse_stream_content(content, source_description="test stream")

    assert len(records) == 2
    assert "Skipped corrupt JSONL line in test stream" in captured.getvalue()


def test_schema_violating_line_is_a_build_error() -> None:
    bad_step = {"type": "step", "event_id": "x", "emitter": _EMITTER}
    content = json.dumps(_header()) + "\n" + json.dumps(bad_step)

    with pytest.raises(TrajectoryBuildError, match="violates the record schema"):
        parse_stream_content(content, source_description="test stream")


def test_unmatched_result_is_preserved_on_nearest_preceding_agent_step() -> None:
    records = _parse(
        [
            _header(),
            _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["call_a"]),
            _observation("o1", "2026-06-09T12:00:02Z", "call_a", "out a"),
            _observation("o2", "2026-06-09T12:00:03Z", "call_missing", "orphan output"),
        ]
    )

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    assert any("unknown tool call 'call_missing'" in warning for warning in result.warnings)
    agent_step = result.trajectory.steps[0]
    assert agent_step.observation is not None
    orphan = agent_step.observation.results[1]
    # The vendored document validator rejects unknown source_call_ids, so the id
    # moves under extra and the result is flagged as unmatched.
    assert orphan.source_call_id is None
    assert orphan.content == "orphan output"
    assert orphan.extra is not None
    assert orphan.extra["unmatched"] is True
    assert orphan.extra["source_call_id"] == "call_missing"


def test_unmatched_result_with_no_agent_step_is_dropped_with_warning() -> None:
    records = _parse(
        [
            _header(),
            _user_step("u1-user", "2026-06-09T12:00:00Z", "hi"),
            _observation("o1", "2026-06-09T12:00:01Z", "call_missing", "orphan output"),
        ]
    )

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    assert any("Dropped observation result" in warning for warning in result.warnings)
    assert result.trajectory.steps[0].observation is None


def test_resolved_subagent_is_embedded_and_referenced() -> None:
    subagent_records = _parse(
        [
            _header(),
            _user_step("su1-user", "2026-06-09T12:00:02Z", "subtask prompt"),
        ]
    )
    subagent_result = build_trajectory_from_records(
        subagent_records,
        TrajectoryEnrichment(
            agent_name="claude",
            agent_version="unknown",
            session_id="child-5678",
            trajectory_id="child-5678",
        ),
        {},
    )
    parent_records = _parse(
        [
            _header(),
            _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["toolu_task"]),
            _observation("o1", "2026-06-09T12:00:05Z", "toolu_task", "subagent finished: summary text"),
        ]
    )

    result = build_trajectory_from_records(
        parent_records,
        _make_enrichment(),
        {"toolu_task": EmbeddedSubagent(trajectory=subagent_result.trajectory, subagent_kind=MNGR_SUBAGENT_KIND)},
    )

    trajectory = result.trajectory
    assert trajectory.subagent_trajectories is not None
    embedded = trajectory.subagent_trajectories[0]
    assert embedded.trajectory_id == "child-5678"
    assert embedded.extra is not None and embedded.extra["subagent_kind"] == "mngr"
    delegating_observation = trajectory.steps[0].observation
    assert delegating_observation is not None
    delegating_result = delegating_observation.results[0]
    assert delegating_result.content == "subagent finished: summary text"
    assert delegating_result.subagent_trajectory_ref is not None
    ref = delegating_result.subagent_trajectory_ref[0]
    assert ref.trajectory_id == "child-5678"
    assert ref.extra is not None and ref.extra["subagent_kind"] == "mngr"
    # The whole embedded document still validates.
    assert Trajectory.model_validate(trajectory.to_json_dict()) == trajectory


def test_subagent_without_matching_tool_call_produces_a_warning() -> None:
    subagent_records = _parse([_header(), _user_step("su1-user", "2026-06-09T12:00:02Z", "subtask")])
    subagent_result = build_trajectory_from_records(
        subagent_records,
        TrajectoryEnrichment(
            agent_name="claude",
            agent_version="unknown",
            session_id="child-1",
            trajectory_id="child-1",
        ),
        {},
    )
    parent_records = _parse([_header(), _user_step("u1-user", "2026-06-09T12:00:00Z", "hi")])

    result = build_trajectory_from_records(
        parent_records,
        _make_enrichment(),
        {"toolu_gone": EmbeddedSubagent(trajectory=subagent_result.trajectory, subagent_kind=MNGR_SUBAGENT_KIND)},
    )

    assert any("no matching tool call" in warning for warning in result.warnings)
    assert result.trajectory.subagent_trajectories is None


def test_duplicate_tool_call_id_attaches_to_later_step_with_warning() -> None:
    records = _parse(
        [
            _header(),
            _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["call_dup"]),
            _agent_step_with_tool_calls("a2-assistant", "2026-06-09T12:00:02Z", ["call_dup"]),
            _observation("o1", "2026-06-09T12:00:03Z", "call_dup", "late output"),
        ]
    )

    result = build_trajectory_from_records(records, _make_enrichment(), {})

    assert any("Duplicate tool_call_id" in warning for warning in result.warnings)
    assert result.trajectory.steps[0].observation is None
    later_step = result.trajectory.steps[1]
    assert later_step.observation is not None
    assert later_step.observation.results[0].content == "late output"


def test_subagent_with_no_result_yet_is_embedded_with_a_pending_marker() -> None:
    subagent_records = _parse([_header(), _user_step("su1-user", "2026-06-09T12:00:02Z", "subtask")])
    subagent_result = build_trajectory_from_records(
        subagent_records,
        TrajectoryEnrichment(
            agent_name="claude",
            agent_version="unknown",
            session_id="child-pending",
            trajectory_id="child-pending",
        ),
        {},
    )
    # The delegating tool call is in the stream, but its observation has not arrived.
    parent_records = _parse(
        [_header(), _agent_step_with_tool_calls("a1-assistant", "2026-06-09T12:00:01Z", ["toolu_task"])]
    )

    result = build_trajectory_from_records(
        parent_records,
        _make_enrichment(),
        {"toolu_task": EmbeddedSubagent(trajectory=subagent_result.trajectory, subagent_kind=MNGR_SUBAGENT_KIND)},
    )

    assert any("has no result yet" in warning for warning in result.warnings)
    trajectory = result.trajectory
    assert trajectory.subagent_trajectories is not None
    assert trajectory.subagent_trajectories[0].trajectory_id == "child-pending"
    delegating_observation = trajectory.steps[0].observation
    assert delegating_observation is not None
    pending_result = delegating_observation.results[0]
    assert pending_result.source_call_id == "toolu_task"
    assert pending_result.content is None
    assert pending_result.extra is not None and pending_result.extra["subagent_result_pending"] is True
    assert pending_result.subagent_trajectory_ref is not None
    assert pending_result.subagent_trajectory_ref[0].trajectory_id == "child-pending"
    assert Trajectory.model_validate(trajectory.to_json_dict()) == trajectory
