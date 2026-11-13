from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import wait_for_records
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import content_text
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import wait_for_native_session_file
from imbue.mngr_claude.testing import wait_for_turn_to_end

_GROUPING_CONSUMER: Final[str] = (
    "common_transcript_convert._group_assistant_records, which builds one step per message.id"
)
_FAN_OUT_PROMPT: Final[str] = (
    "In a single reply, first write the word PLANNING, then call the Bash tool twice in parallel: one call "
    "runs exactly `echo FANOUT-ONE` and the other runs exactly `echo FANOUT-TWO`. After both finish, reply "
    "with just DONE."
)


def _agent_steps_for(common_records: Sequence[Mapping[str, Any]], message_id: str) -> list[Mapping[str, Any]]:
    return [
        record
        for record in common_records
        if is_step_from(record, "agent") and (record.get("extra") or {}).get("message_id") == message_id
    ]


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_response_written_over_several_lines_becomes_one_agent_step(tmp_path: Path) -> None:
    """A response claude writes over several assistant lines reaches the common transcript as one agent step.

    Pins how claude writes one API response: assistant records, one per content block, that share a
    ``message.id`` and each repeat the response's ``usage``.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-fanout-{get_short_random_string()}"
    drift = drift_note(ctx, "the assistant records of one response")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, _FAN_OUT_PROMPT)
        wait_for_turn_to_end(profile, ctx, _FAN_OUT_PROMPT, drift)

        lines_by_message_id: dict[str, list[dict[str, Any]]] = {}
        for record in read_session_records(wait_for_native_session_file(ctx)):
            message_id = (record.get("message") or {}).get("id")
            if record.get("type") == "assistant" and isinstance(message_id, str) and message_id:
                lines_by_message_id.setdefault(message_id, []).append(record)
        fanned_out_lines_by_message_id = {
            message_id: lines for message_id, lines in lines_by_message_id.items() if len(lines) > 1
        }
        assert fanned_out_lines_by_message_id, (
            f"no response to a prompt asking for text and two tool calls in one reply was written over more than one "
            f"assistant record sharing a message.id {drift}, or the model answered in separate replies; "
            f"consumer: {_GROUPING_CONSUMER}"
        )
        for message_id, lines in fanned_out_lines_by_message_id.items():
            usages = [line["message"].get("usage") for line in lines]
            assert all(isinstance(usage, dict) for usage in usages) and all(u == usages[0] for u in usages), (
                f"the {len(lines)} assistant records of response {message_id} do not each carry the same "
                f"message.usage {drift}; consumer: {_GROUPING_CONSUMER}, which reads one usage per response"
            )

        common_records = wait_for_records(
            ctx.host_dir,
            profile.common_transcript_subdir,
            lambda rs: all(_agent_steps_for(rs, message_id) for message_id in fanned_out_lines_by_message_id),
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            description=f"the fanned-out responses never all reached the common transcript as agent steps {drift}; "
            f"consumer: {_GROUPING_CONSUMER}",
        )
        for message_id, lines in fanned_out_lines_by_message_id.items():
            steps = _agent_steps_for(common_records, message_id)
            assert len(steps) == 1, (
                f"response {message_id}, written over {len(lines)} assistant records, became {len(steps)} agent steps, "
                f"not one {drift}; consumer: {_GROUPING_CONSUMER}"
            )


_TOOL_RESULT_CONSUMER: Final[str] = "common_transcript_convert._build_user_records (tool_result to observation)"


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_failed_tool_call_reaches_the_common_transcript_as_an_error(tmp_path: Path) -> None:
    """A Bash call that exits non-zero reaches the common transcript as an error observation carrying its output.

    Pins the tool_result block the converter reads for it: ``tool_use_id`` naming the call,
    ``is_error`` true, and ``content`` as a string or text blocks carrying the output.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-toolerror-{get_short_random_string()}"
    token = f"TOOLFAIL-{get_short_random_string()}"
    drift = drift_note(ctx, "the tool_result block")
    prompt = f"Use the Bash tool to run exactly: echo {token} && exit 3. Then reply with just DONE."
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, prompt)
        wait_for_turn_to_end(profile, ctx, prompt, drift)

        records = read_session_records(wait_for_native_session_file(ctx))
        bash_call_ids = [
            block["id"]
            for record in records
            if record.get("type") == "assistant"
            for block in (record.get("message") or {}).get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"
        ]
        results = [
            block
            for record in records
            if record.get("type") == "user" and isinstance((record.get("message") or {}).get("content"), list)
            for block in record["message"]["content"]
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") in bash_call_ids
        ]
        assert bash_call_ids and results, (
            f"no tool_result block names the Bash call's id {drift}; calls: {bash_call_ids}; "
            f"consumer: {_TOOL_RESULT_CONSUMER}"
        )
        failed = [block for block in results if token in content_text(block.get("content"))]
        assert failed and failed[0].get("is_error") is True, (
            f"the failed Bash call's tool_result does not carry is_error true and its output as content "
            f"({[(b.get('is_error'), type(b.get('content')).__name__) for b in results]}) {drift}; "
            f"consumer: {_TOOL_RESULT_CONSUMER}"
        )
        call_id = failed[0]["tool_use_id"]

        def error_observation(common_records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
            for record in common_records:
                if record.get("type") != "observation":
                    continue
                for result in record.get("results") or []:
                    if result.get("source_call_id") == call_id:
                        return result
            return None

        common_records = wait_for_records(
            ctx.host_dir,
            profile.common_transcript_subdir,
            lambda rs: error_observation(rs) is not None,
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            description=f"the failed Bash call's result never reached the common transcript {drift}; "
            f"consumer: {_TOOL_RESULT_CONSUMER}",
        )
        observation = error_observation(common_records)
        assert observation is not None
        extra = observation.get("extra") or {}
        assert (
            extra.get("is_error") is True
            and extra.get("tool_name") == "Bash"
            and token in str(observation.get("content"))
        ), (
            f"the failed Bash call reached the common transcript as {observation}, not an error result from Bash "
            f"carrying its output {drift}; consumer: {_TOOL_RESULT_CONSUMER}"
        )
