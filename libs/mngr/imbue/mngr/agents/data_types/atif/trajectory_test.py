"""Tests pinning the behavior of the vendored ATIF models (see README.md here).

These assert the validation rules the rest of mngr relies on (sequential step ids,
observation/tool-call reference integrity, embedded-subagent identity, resolvable
subagent refs), so a future re-vendor that changes them fails loudly.
"""

from typing import Any

import pydantic
import pytest

from imbue.mngr.agents.data_types.atif.agent import Agent
from imbue.mngr.agents.data_types.atif.content import ContentPart
from imbue.mngr.agents.data_types.atif.content import ImageSource
from imbue.mngr.agents.data_types.atif.metrics import Metrics
from imbue.mngr.agents.data_types.atif.observation import Observation
from imbue.mngr.agents.data_types.atif.observation_result import ObservationResult
from imbue.mngr.agents.data_types.atif.step import Step
from imbue.mngr.agents.data_types.atif.subagent_trajectory_ref import SubagentTrajectoryRef
from imbue.mngr.agents.data_types.atif.tool_call import ToolCall
from imbue.mngr.agents.data_types.atif.trajectory import Trajectory

# The multi-step example document from ATIF RFC section IV (rfcs/0001-trajectory-format.md
# in the harbor repo), trimmed of the token-id/logprob arrays for readability.
_RFC_EXAMPLE_TRAJECTORY: dict[str, Any] = {
    "schema_version": "ATIF-v1.5",
    "session_id": "025B810F-B3A2-4C67-93C0-FE7A142A947A",
    "agent": {
        "name": "harbor-agent",
        "version": "1.0.0",
        "model_name": "gemini-2.5-flash",
        "tool_definitions": [
            {
                "type": "function",
                "function": {
                    "name": "financial_search",
                    "description": "Search for financial data for a given stock ticker",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string", "description": "Stock ticker symbol"},
                            "metric": {"type": "string", "description": "The financial metric to retrieve"},
                        },
                        "required": ["ticker", "metric"],
                    },
                },
            }
        ],
        "extra": {},
    },
    "notes": "Initial test trajectory for financial data retrieval.",
    "extra": {},
    "final_metrics": {
        "total_prompt_tokens": 1120,
        "total_completion_tokens": 124,
        "total_cached_tokens": 200,
        "total_cost_usd": 0.00078,
        "total_steps": 3,
        "extra": {},
    },
    "steps": [
        {
            "step_id": 1,
            "timestamp": "2025-10-11T10:30:00Z",
            "source": "user",
            "message": "What is the current trading price of Alphabet (GOOGL)?",
            "extra": {},
        },
        {
            "step_id": 2,
            "timestamp": "2025-10-11T10:30:02Z",
            "source": "agent",
            "model_name": "gemini-2.5-flash",
            "reasoning_effort": "medium",
            "message": "I will search for the current trading price and volume for GOOGL.",
            "reasoning_content": "The request requires two data points: price and volume.",
            "tool_calls": [
                {
                    "tool_call_id": "call_price_1",
                    "function_name": "financial_search",
                    "arguments": {"ticker": "GOOGL", "metric": "price"},
                },
                {
                    "tool_call_id": "call_volume_2",
                    "function_name": "financial_search",
                    "arguments": {"ticker": "GOOGL", "metric": "volume"},
                },
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "call_price_1",
                        "content": "GOOGL is currently trading at $185.35 (Close: 10/11/2025)",
                    },
                    {"source_call_id": "call_volume_2", "content": "GOOGL volume: 1.5M shares traded."},
                ]
            },
            "metrics": {
                "prompt_tokens": 520,
                "completion_tokens": 80,
                "cached_tokens": 200,
                "cost_usd": 0.00045,
            },
        },
        {
            "step_id": 3,
            "timestamp": "2025-10-11T10:30:05Z",
            "source": "agent",
            "model_name": "gemini-2.5-flash",
            "reasoning_effort": "low",
            "message": "As of October 11, 2025, Alphabet (GOOGL) is trading at $185.35.",
            "reasoning_content": "The previous step retrieved all necessary data.",
            "metrics": {
                "prompt_tokens": 600,
                "completion_tokens": 44,
                "cost_usd": 0.00033,
                "extra": {"reasoning_tokens": 12},
            },
        },
    ],
}


def _make_minimal_agent() -> Agent:
    return Agent(name="test-agent", version="unknown")


def _make_user_step(step_id: int) -> Step:
    return Step(step_id=step_id, source="user", message="hello")


def _make_subagent() -> Trajectory:
    return Trajectory(
        trajectory_id="sub-1",
        agent=_make_minimal_agent(),
        steps=[_make_user_step(1)],
    )


def test_rfc_example_trajectory_validates() -> None:
    trajectory = Trajectory.model_validate(_RFC_EXAMPLE_TRAJECTORY)

    assert trajectory.schema_version == "ATIF-v1.5"
    assert len(trajectory.steps) == 3
    assert trajectory.steps[1].tool_calls is not None
    assert trajectory.steps[1].tool_calls[0].arguments == {"ticker": "GOOGL", "metric": "price"}


def test_rfc_example_round_trips_through_to_json_dict() -> None:
    trajectory = Trajectory.model_validate(_RFC_EXAMPLE_TRAJECTORY)

    dumped = trajectory.to_json_dict()
    revalidated = Trajectory.model_validate(dumped)

    assert revalidated == trajectory
    # to_json_dict drops None-valued fields entirely.
    assert "continued_trajectory_ref" not in dumped
    assert "reasoning_content" not in dumped["steps"][0]


def test_non_sequential_step_ids_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="expected 2"):
        Trajectory(
            agent=_make_minimal_agent(),
            steps=[_make_user_step(1), _make_user_step(3)],
        )


def test_step_ids_must_start_at_one() -> None:
    with pytest.raises(pydantic.ValidationError, match="expected 1"):
        Trajectory(agent=_make_minimal_agent(), steps=[_make_user_step(2)])


def test_observation_result_referencing_unknown_tool_call_is_rejected() -> None:
    agent_step = Step(
        step_id=1,
        source="agent",
        message="",
        tool_calls=[ToolCall(tool_call_id="call_a", function_name="Bash", arguments={})],
        observation=Observation(results=[ObservationResult(source_call_id="call_other", content="out")]),
    )

    with pytest.raises(pydantic.ValidationError, match="call_other"):
        Trajectory(agent=_make_minimal_agent(), steps=[agent_step])


def test_agent_only_fields_are_rejected_on_user_steps() -> None:
    with pytest.raises(pydantic.ValidationError, match="only applicable when source is 'agent'"):
        Step(step_id=1, source="user", message="hi", reasoning_content="thinking")


def test_metrics_must_be_absent_when_llm_call_count_is_zero() -> None:
    with pytest.raises(pydantic.ValidationError, match="llm_call_count"):
        Step(
            step_id=1,
            source="agent",
            message="",
            llm_call_count=0,
            metrics=Metrics(prompt_tokens=1),
        )


def test_embedded_subagent_without_trajectory_id_is_rejected() -> None:
    subagent = Trajectory(agent=_make_minimal_agent(), steps=[_make_user_step(1)])

    with pytest.raises(pydantic.ValidationError, match="trajectory_id is required"):
        Trajectory(
            agent=_make_minimal_agent(),
            steps=[_make_user_step(1)],
            subagent_trajectories=[subagent],
        )


def test_embedded_subagents_with_duplicate_trajectory_ids_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="not unique"):
        Trajectory(
            agent=_make_minimal_agent(),
            steps=[_make_user_step(1)],
            subagent_trajectories=[_make_subagent(), _make_subagent()],
        )


def test_subagent_ref_without_resolution_key_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="resolvable"):
        SubagentTrajectoryRef(session_id="run-1")


def test_subagent_ref_with_trajectory_id_alone_is_resolvable() -> None:
    ref = SubagentTrajectoryRef(trajectory_id="sub-1")

    assert ref.trajectory_id == "sub-1"
    assert ref.trajectory_path is None


def test_content_part_requires_text_field_for_text_type() -> None:
    with pytest.raises(pydantic.ValidationError, match="'text' field is required"):
        ContentPart(type="text")


def test_content_part_rejects_text_field_for_image_type() -> None:
    with pytest.raises(pydantic.ValidationError, match="not allowed"):
        ContentPart(
            type="image",
            text="nope",
            source=ImageSource(media_type="image/png", path="images/x.png"),
        )


def test_invalid_timestamps_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="Invalid ISO 8601 timestamp"):
        Step(step_id=1, source="user", message="hi", timestamp="not-a-timestamp")


def test_schema_version_defaults_to_the_pinned_atif_revision() -> None:
    trajectory = Trajectory(agent=_make_minimal_agent(), steps=[_make_user_step(1)])

    assert trajectory.schema_version == "ATIF-v1.7"


def test_unknown_fields_are_rejected_at_the_trajectory_level() -> None:
    with pytest.raises(pydantic.ValidationError, match="bogus_field"):
        Trajectory.model_validate(
            {
                "agent": {"name": "a", "version": "1"},
                "steps": [{"step_id": 1, "source": "user", "message": "m"}],
                "bogus_field": True,
            }
        )


def test_unknown_fields_are_rejected_inside_a_step() -> None:
    with pytest.raises(pydantic.ValidationError, match="bogus_field"):
        Trajectory.model_validate(
            {
                "agent": {"name": "a", "version": "1"},
                "steps": [{"step_id": 1, "source": "user", "message": "m", "bogus_field": True}],
            }
        )


def test_unknown_fields_are_rejected_inside_a_tool_call() -> None:
    with pytest.raises(pydantic.ValidationError, match="bogus_field"):
        Trajectory.model_validate(
            {
                "agent": {"name": "a", "version": "1"},
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "m",
                        "tool_calls": [
                            {
                                "tool_call_id": "call_a",
                                "function_name": "Bash",
                                "arguments": {},
                                "bogus_field": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_has_multimodal_content_detects_image_parts() -> None:
    text_only = Trajectory(agent=_make_minimal_agent(), steps=[_make_user_step(1)])
    with_image = Trajectory(
        agent=_make_minimal_agent(),
        steps=[
            Step(
                step_id=1,
                source="user",
                message=[
                    ContentPart(type="text", text="look at this"),
                    ContentPart(
                        type="image",
                        source=ImageSource(media_type="image/png", path="images/screenshot.png"),
                    ),
                ],
            )
        ],
    )

    assert text_only.has_multimodal_content() is False
    assert with_image.has_multimodal_content() is True
