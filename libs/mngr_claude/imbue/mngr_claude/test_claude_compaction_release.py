from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import count_user_steps_containing
from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import step_message
from imbue.mngr.agents.agent_release_testing import wait_for_records
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_claude.claude_config import COMPACTING_MARKER_FILENAME
from imbue.mngr_claude.claude_config import LAST_COMPACTION_FILENAME
from imbue.mngr_claude.dialogs import COMPACT_COMMAND
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import PROMPT_TOKEN_USAGE_FIELDS
from imbue.mngr_claude.testing import RAW_TRANSCRIPT_RELPATH
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import append_project_settings
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import session_message_text
from imbue.mngr_claude.testing import wait_for_native_session_file
from imbue.mngr_claude.testing import wait_for_session_record
from imbue.mngr_claude.testing import wait_for_turn_to_end

_AUTOCOMPACT_CHECK_TIMEOUT_SECONDS: Final[float] = 120.0
_STREAM_TIMEOUT_SECONDS: Final[float] = 60.0
_SUMMARY_HEAD_CHARS: Final[int] = 80
_FIRST_PROMPT: Final[str] = "Reply with just OK."
_SECOND_PROMPT: Final[str] = "Reply with just OK again."
_CONTEXT_SIZE_CONSUMER: Final[str] = (
    "mngr_claude.compaction.extract_context_tokens_from_record via get_agent_context_tokens"
)
_BOUNDARY_CONSUMER: Final[str] = (
    "mngr_claude.compaction.is_compact_boundary_record in ClaudeCompactionTranscriptScanner"
)
_SUMMARY_CONSUMER: Final[str] = "common_transcript_convert._build_user_records (isCompactSummary)"
_COMPACT_HOOK_EVENTS: Final[tuple[str, ...]] = ("PreCompact", "PostCompact")
_MANUAL_TRIGGER: Final[str] = "manual"
_COMMAND_NAME_TAG: Final[str] = "<command-name>"
# Independent of the converter's own tag list, so disabling that list cannot blind the check.
_LEADING_MARKUP_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^\s*<[a-z][a-z-]*>")
_COMPACT_HOOKS_CONSUMER: Final[str] = (
    "mngr_claude.claude_config's PreCompact/PostCompact hooks (_READ_COMPACTION_TRIGGER)"
)
_PLUMBING_CONSUMER: Final[str] = "common_transcript_convert._is_command_plumbing (_COMMAND_PLUMBING_PREFIXES)"


def _autocompact_would_compact(ctx: AgentReleaseContext, agent_name: str, min_context_tokens: int, drift: str) -> bool:
    """Whether ``mngr autocompact check`` lists ``agent_name`` when compaction needs ``min_context_tokens``."""
    check = run_mngr_subprocess(
        "autocompact",
        "check",
        agent_name,
        "--format",
        "json",
        "-S",
        f"plugins.autocompact.min_context_tokens={min_context_tokens}",
        env=dict(ctx.env),
        timeout=_AUTOCOMPACT_CHECK_TIMEOUT_SECONDS,
        verbose=0,
    )
    assert check.returncode == 0, (
        f"mngr autocompact check failed {drift}; consumer: mngr_autocompact.cli check:\n{check.stdout}\n{check.stderr}"
    )
    return agent_name in json.loads(check.stdout)["agents"]


def _has_top_level_usage(record: Mapping[str, Any]) -> bool:
    return isinstance(record.get("usage"), dict)


def _is_reply_with_usage(record: Mapping[str, Any]) -> bool:
    return record.get("type") == "assistant" and isinstance((record.get("message") or {}).get("usage"), dict)


# Independent of compaction.is_compact_boundary_record, so disabling that reader cannot blind the check.
def _is_compact_boundary(record: Mapping[str, Any]) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


def _wait_for_compact_boundary(session_file: Path, drift: str) -> dict[str, Any]:
    return wait_for_session_record(
        session_file,
        _is_compact_boundary,
        failure=f"{COMPACT_COMMAND} wrote no system record with subtype compact_boundary {drift}; "
        f"consumer: {_BOUNDARY_CONSUMER}",
    )


def _is_compaction_step(record: Mapping[str, Any]) -> bool:
    context_management = (record.get("extra") or {}).get("context_management") or {}
    return is_step_from(record, "system") and context_management.get("type") == "compaction"


def _compaction_step_content(record: Mapping[str, Any]) -> str:
    results = (record.get("observation") or {}).get("results") or [{}]
    return str(results[0].get("content", ""))


def _summary_head(summary: Mapping[str, Any]) -> str:
    """The start of the summary's first non-blank text, as its compaction step in the common transcript carries it."""
    content = (summary.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    texts = (block.get("text") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    return next((text.strip()[:_SUMMARY_HEAD_CHARS] for text in texts if isinstance(text, str) and text.strip()), "")


def _wait_until_streamed(ctx: AgentReleaseContext, record_uuid: str, drift: str) -> None:
    raw_transcript = agent_state_dir(ctx.host_dir) / RAW_TRANSCRIPT_RELPATH
    assert poll_until(
        lambda: raw_transcript.exists()
        and any(record.get("uuid") == record_uuid for record in read_session_records(raw_transcript)),
        timeout=_STREAM_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), f"record {record_uuid} never reached {RAW_TRANSCRIPT_RELPATH} {drift}; consumer: stream_transcript.sh"


def _wait_for_idle_reply(
    profile: ClaudeReleaseProfile,
    ctx: AgentReleaseContext,
    session_file: Path,
    prompt_text: str,
    earlier_uuids: frozenset[str],
    drift: str,
) -> dict[str, Any]:
    """Wait for a new usage-bearing reply and the end of ``prompt_text``'s turn; return the newest reply with usage.

    Fails if a newer record carries a top-level usage or the reply's timestamp is not ISO-8601.
    """
    wait_for_session_record(
        session_file,
        lambda record: _is_reply_with_usage(record) and str(record.get("uuid")) not in earlier_uuids,
        failure=f"no new assistant record carries message.usage {drift}; consumer: {_CONTEXT_SIZE_CONSUMER}",
    )
    wait_for_turn_to_end(profile, ctx, prompt_text, drift)
    records = read_session_records(session_file)
    reply_index = max(index for index, record in enumerate(records) if _is_reply_with_usage(record))
    newer_usage = [record.get("type") for record in records[reply_index + 1 :] if _has_top_level_usage(record)]
    assert not newer_usage, (
        f"records of type {newer_usage} carry a top-level usage newer than the reply {drift}; "
        "consumer: ClaudeCompactionTranscriptScanner, which sizes the context by the newest usage of any record"
    )
    reply = records[reply_index]
    timestamp = reply.get("timestamp")
    timestamp_failure = (
        f"the reply's timestamp {timestamp!r} is not ISO-8601 {drift}; "
        "consumer: mngr_claude.compaction.extract_assistant_timestamp_from_record"
    )
    assert isinstance(timestamp, str), timestamp_failure
    try:
        datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError:
        pytest.fail(timestamp_failure)
    return reply


def _context_tokens_in_usage(reply: Mapping[str, Any], drift: str) -> int:
    usage = reply["message"]["usage"]
    missing = [field for field in PROMPT_TOKEN_USAGE_FIELDS if not isinstance(usage.get(field), int)]
    assert not missing, (
        f"the reply's message.usage has no integer {missing} (keys: {sorted(usage)}) {drift}; "
        f"consumer: {_CONTEXT_SIZE_CONSUMER}"
    )
    total = sum(usage[field] for field in PROMPT_TOKEN_USAGE_FIELDS)
    assert total > 0, f"the reply's message.usage counters sum to {total} {drift}; consumer: {_CONTEXT_SIZE_CONSUMER}"
    return total


def _assert_autocompact_sizes_agent_at(
    ctx: AgentReleaseContext, agent_name: str, context_tokens: int, drift: str
) -> None:
    usage_sum = " + ".join(PROMPT_TOKEN_USAGE_FIELDS)
    assert _autocompact_would_compact(ctx, agent_name, context_tokens, drift), (
        f"mngr autocompact would not compact the idle agent at min_context_tokens={context_tokens}, the "
        f"{usage_sum} of its newest reply, so it read no context size or a smaller one {drift}; "
        f"consumer: {_CONTEXT_SIZE_CONSUMER}"
    )
    assert not _autocompact_would_compact(ctx, agent_name, context_tokens + 1, drift), (
        f"mngr autocompact would compact the idle agent at min_context_tokens={context_tokens + 1}, so it read a "
        f"context size larger than the newest reply's {usage_sum} of {context_tokens} {drift}; "
        f"consumer: {_CONTEXT_SIZE_CONSUMER}"
    )


def _assert_compaction_hooks_saw_a_manual_trigger(dump_dir: Path, drift: str) -> None:
    """Wait for both compaction hooks' saved payloads, and require each to carry the manual trigger."""
    assert poll_until(
        lambda: {p.get("hook_event_name") for p in read_saved_payloads(dump_dir)} >= set(_COMPACT_HOOK_EVENTS),
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), (
        f"{COMPACT_COMMAND} did not run both {_COMPACT_HOOK_EVENTS} hooks with a JSON payload {drift}; "
        f"consumer: {_COMPACT_HOOKS_CONSUMER}; "
        f"saved hook_event_names: {[p.get('hook_event_name') for p in read_saved_payloads(dump_dir)]}"
    )
    trigger_by_hook_event_name = {p.get("hook_event_name"): p.get("trigger") for p in read_saved_payloads(dump_dir)}
    assert trigger_by_hook_event_name == {event: _MANUAL_TRIGGER for event in _COMPACT_HOOK_EVENTS}, (
        f"the compaction hook payloads' trigger by hook_event_name is {trigger_by_hook_event_name}, not "
        f"{_MANUAL_TRIGGER!r} for each of {_COMPACT_HOOK_EVENTS} {drift}; consumer: {_COMPACT_HOOKS_CONSUMER}"
    )


def _assert_mngr_recorded_a_manual_compaction(ctx: AgentReleaseContext, drift: str) -> None:
    """Wait for mngr's last_compaction.json and the compacting marker's removal, and require a manual trigger."""
    state_dir = agent_state_dir(ctx.host_dir)
    assert poll_until(
        lambda: (state_dir / LAST_COMPACTION_FILENAME).exists()
        and not (state_dir / COMPACTING_MARKER_FILENAME).exists(),
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), (
        f"after {COMPACT_COMMAND} mngr wrote no {LAST_COMPACTION_FILENAME} or left the "
        f"{COMPACTING_MARKER_FILENAME} marker in place {drift}; consumer: {_COMPACT_HOOKS_CONSUMER}"
    )
    last_compaction = json.loads((state_dir / LAST_COMPACTION_FILENAME).read_text())
    assert last_compaction.get("trigger") == _MANUAL_TRIGGER, (
        f"mngr recorded the user's {COMPACT_COMMAND} as {last_compaction}, not a {_MANUAL_TRIGGER!r} "
        f"compaction {drift}; consumer: {_COMPACT_HOOKS_CONSUMER}"
    )


def _assert_no_command_plumbing_steps(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, later_prompt: str, drift: str
) -> None:
    """Wait for ``later_prompt``'s user step, and require no common-transcript step to lead with a markup tag."""
    # Claude writes the plumbing after the summary; a later prompt's user step shows the converter read it.
    common_records_after_next_prompt = wait_for_records(
        ctx.host_dir,
        profile.common_transcript_subdir,
        lambda rs: count_user_steps_containing(rs, later_prompt) >= 1,
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        description=f"the prompt after {COMPACT_COMMAND} never reached the common transcript as a user step "
        f"{drift}; consumer: common_transcript_convert._build_user_records",
    )
    plumbing_steps = [
        step_message(record)[:120]
        for record in common_records_after_next_prompt
        if record.get("type") == "step" and _LEADING_MARKUP_TAG_RE.match(step_message(record))
    ]
    assert not plumbing_steps, (
        f"slash-command plumbing reached the common transcript as steps {plumbing_steps} {drift}; "
        f"consumer: {_PLUMBING_CONSUMER}"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_compact_is_recorded_as_manual_and_autocompact_sizes_the_agent_around_it(tmp_path: Path) -> None:
    """A ``/compact`` is recorded as manual, and ``mngr autocompact`` sizes the idle agent around it.

    Autocompact sizes the agent by its newest reply's usage, and as unknown from the ``/compact``
    until the next reply. mngr also keeps the command's plumbing out of the common transcript.

    Pins what mngr's compaction readers take from claude's session records: the reply's
    ``message.usage`` input, cache-read and cache-creation counters and its timestamp, the
    ``compact_boundary`` system record ``/compact`` writes, and the ``isCompactSummary`` user
    record the common-transcript converter turns into a compaction step. Also pins the
    PreCompact/PostCompact hook payloads' ``trigger``, which mngr records in last_compaction.json
    and the compacting marker, and the ``<command-name>`` plumbing records the slash command
    writes, which the converter drops by their leading tag.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    # Proactive autocompact with no idle delay: a 1-minute cache TTL less a 1-minute epsilon.
    append_project_settings(
        ctx, '\n[plugins.autocompact]\nmode = "proactive_timer"\ncache_ttl_minutes = 1\nepsilon_offset_minutes = 1\n'
    )
    dump_dir = tmp_path / "compact-hook-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, _COMPACT_HOOK_EVENTS, None)
    agent_name = f"claude-autocompact-{get_short_random_string()}"
    drift = drift_note(ctx, "the usage, compaction or command records or the compaction hook payloads")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, _FIRST_PROMPT)
        session_file = wait_for_native_session_file(ctx)
        reply = _wait_for_idle_reply(profile, ctx, session_file, _FIRST_PROMPT, frozenset(), drift)
        context_tokens = _context_tokens_in_usage(reply, drift)
        _wait_until_streamed(ctx, str(reply["uuid"]), drift)
        _assert_autocompact_sizes_agent_at(ctx, agent_name, context_tokens, drift)

        send_expecting_success(profile, ctx, agent_name, COMPACT_COMMAND)
        boundary = _wait_for_compact_boundary(session_file, drift)
        _assert_compaction_hooks_saw_a_manual_trigger(dump_dir, drift)
        wait_for_session_record(
            session_file,
            lambda record: record.get("type") == "user"
            and session_message_text(record).lstrip().startswith(_COMMAND_NAME_TAG),
            failure=f"{COMPACT_COMMAND} wrote no user record starting with {_COMMAND_NAME_TAG!r} {drift}; "
            f"consumer: {_PLUMBING_CONSUMER}",
        )
        _assert_mngr_recorded_a_manual_compaction(ctx, drift)

        summary = wait_for_session_record(
            session_file,
            lambda record: record.get("type") == "user" and record.get("isCompactSummary") is True,
            failure=f"{COMPACT_COMMAND} wrote no user record with isCompactSummary true {drift}; "
            f"consumer: {_SUMMARY_CONSUMER}",
        )
        summary_head = _summary_head(summary)
        assert summary.get("uuid") and summary.get("timestamp") and summary_head, (
            f"the isCompactSummary record lacks a uuid, a timestamp or any text in message.content {drift}; "
            f"consumer: {_SUMMARY_CONSUMER}, which drops a record without a uuid or a timestamp"
        )
        _wait_until_streamed(ctx, str(boundary["uuid"]), drift)
        records = read_session_records(session_file)
        after_boundary = records[next(i for i, r in enumerate(records) if r.get("uuid") == boundary["uuid"]) + 1 :]
        assert not any(_is_reply_with_usage(r) or _has_top_level_usage(r) for r in after_boundary), (
            f"claude wrote a usage-bearing record after the compact boundary while idle {drift}; "
            f"consumer: {_BOUNDARY_CONSUMER}, which reads the context size as unknown until the next reply"
        )
        assert _autocompact_would_compact(ctx, agent_name, 0, drift), (
            f"after {COMPACT_COMMAND}, mngr autocompact would not compact the agent even with no context-size gate, "
            f"so it no longer reads the agent as idle and running {drift}; consumers: "
            "mngr_claude.compaction.get_agent_idle_since (the active marker: /compact must fire no UserPromptSubmit) "
            "and is_running"
        )
        assert not _autocompact_would_compact(ctx, agent_name, 1, drift), (
            f"mngr autocompact would compact an agent the user just compacted: it sized it by the pre-compaction "
            f"reply instead of as unknown until the next reply {drift}; consumer: {_BOUNDARY_CONSUMER}"
        )
        common_records = wait_for_records(
            ctx.host_dir,
            profile.common_transcript_subdir,
            lambda rs: any(_is_compaction_step(r) and summary_head in _compaction_step_content(r) for r in rs),
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            description=f"{COMPACT_COMMAND}'s summary never reached the common transcript as a compaction step "
            f"{drift}; consumer: {_SUMMARY_CONSUMER}",
        )
        assert count_user_steps_containing(common_records, summary_head) == 0, (
            f"the compaction summary reached the common transcript as a user turn {drift}; consumer: {_SUMMARY_CONSUMER}"
        )

        earlier_uuids = frozenset(str(record.get("uuid")) for record in read_session_records(session_file))
        send_expecting_success(profile, ctx, agent_name, _SECOND_PROMPT)
        next_reply = _wait_for_idle_reply(profile, ctx, session_file, _SECOND_PROMPT, earlier_uuids, drift)
        next_context_tokens = _context_tokens_in_usage(next_reply, drift)
        _wait_until_streamed(ctx, str(next_reply["uuid"]), drift)
        _assert_autocompact_sizes_agent_at(ctx, agent_name, next_context_tokens, drift)

        _assert_no_command_plumbing_steps(profile, ctx, _SECOND_PROMPT, drift)
